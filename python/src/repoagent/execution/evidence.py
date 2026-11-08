"""完整源码行的覆盖与读取窗口；不推断语义调用关系，也不改变既有证据身份。"""

import re

from repoagent.execution.contracts import Evidence
from repoagent.execution.control import estimate
from repoagent.tools.store import source_lines


def uncovered_segments(
    existing: list[Evidence], path: str, start: int, lines: list[str]
):
    """返回复用ID及未覆盖的连续行段；重叠原文不一致时拒绝合并。"""
    covered = set()
    reused = []
    for item in existing:
        if item.path != path:
            continue
        left, right = (
            max(start, item.start_line),
            min(start + len(lines) - 1, item.end_line),
        )
        if left > right:
            continue
        original = source_lines(item.text)
        if (
            lines[left - start : right - start + 1]
            != original[left - item.start_line : right - item.start_line + 1]
        ):
            raise ValueError("Overlapping evidence differs")
        covered.update(range(left, right + 1))
        reused.append(item.evidence_id)
    segments = []
    offset = 0
    while offset < len(lines):
        if start + offset in covered:
            offset += 1
            continue
        end = offset + 1
        while end < len(lines) and start + end not in covered:
            end += 1
        segments.append((start + offset, lines[offset:end]))
        offset = end
    return reused, segments


def ordered_hits(hits: list[dict], strategy: str) -> list[dict]:
    """保持当前检索命中顺序。"""
    return hits


def read_window(hit: dict, kind: str, strategy: str) -> tuple[int, int]:
    """当前策略保持60行窗口，长方法由工具续读。"""
    start = max(1, hit["start_line"] - 3)
    end = min(hit.get("end_line", start) + 3, start + 59)
    if kind == "path":
        return start, start + 59
    if "enclosing_type_start_line" in hit:
        start = max(1, hit["enclosing_type_start_line"] - 3, end - 59)
        end = min(hit["enclosing_type_end_line"] + 3, start + 59)
    return start, end


class EvidenceManager:
    """Own cumulative source text; request selection never changes this collection."""

    def __init__(self, state, limits, trace, limitations):
        self.state, self.limits = state, limits
        self.trace, self.limitations = trace, limitations
        self.pinned = []
        self.active_ids = []
        self.revision = 0
        self.catalog_next_cursor = None

    def select(self, question, budget=None):
        budget = min(budget or self.limits.active_evidence_bytes, self.limits.max_input_estimate)
        # Dotted/camel identifiers must not be swallowed by adjacent Chinese text.
        identifiers = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", question)
        tokens = {word.lower() for word in identifiers}
        tokens.update(word.lower() for token in identifiers for word in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", token) if len(word) > 2)
        pool = self.state.evidence
        def score(e):
            return (e.evidence_id in self.pinned,
                    (20 if e.evidence_id in identifiers else 0) + sum(min(len(t), 20) for t in tokens if t in (e.path + " " + e.text).lower()),
                    pool.index(e))
        ranked = sorted(pool, key=score, reverse=True)
        # Give each repository one relevant candidate before filling remaining space.
        representatives = []
        for item in ranked:
            if item.repo_id not in {e.repo_id for e in representatives}:
                representatives.append(item)
        order = [e for e in ranked if e.evidence_id in self.pinned]
        order += [e for e in representatives + ranked if e not in order]
        selected = []
        for item in order:
            if item not in selected and estimate([e.model_dump() for e in selected + [item]]) <= budget:
                selected.append(item)
        selected.sort(key=pool.index)
        ids = [e.evidence_id for e in selected]
        if ids != self.active_ids:
            self.revision += 1
            self.active_ids = ids
        return selected

    def catalog(self, cursor=0, budget=None):
        if not 0 <= cursor <= len(self.state.evidence):
            raise ValueError("invalid_evidence_cursor")
        budget = budget or self.limits.cold_catalog_bytes
        result = []
        self.catalog_next_cursor = None
        for index in range(cursor, len(self.state.evidence)):
            item = self.state.evidence[index]
            if item.evidence_id in self.active_ids:
                continue
            row = item.model_dump(exclude={"text"}) | {"citable": False}
            if estimate(result + [row]) > budget:
                self.catalog_next_cursor = index
                break
            result.append(row)
        return result

    def expand(self, evidence_ids=None, catalog_cursor=None):
        if catalog_cursor is not None:
            page = self.catalog(catalog_cursor, max(512, self.limits.observation_estimate - 128))
            return {"cold_catalog": page, "next_cursor": self.catalog_next_cursor, "catalog_only": True, "citable": False}
        evidence_ids = evidence_ids or []
        known = {e.evidence_id: e for e in self.state.evidence}
        if any(identifier not in known for identifier in evidence_ids):
            raise ValueError("unknown_evidence_id")
        required = list(dict.fromkeys(evidence_ids + self.pinned))
        if estimate([known[i].model_dump() for i in required]) > self.limits.active_evidence_bytes:
            raise ValueError("evidence_selection_capacity")
        changed = any(i not in self.active_ids for i in evidence_ids)
        self.pinned = required
        return {"evidence_ids": evidence_ids, "activated": changed, "citable_in_next_request": True}

    def add(self, block: dict) -> list[str]:
        """将read_file返回块加入累计证据，返回可供引用的证据ID列表。

        仅保留完整行；被已有原文范围完整覆盖时复用ID；预算不足从末尾逐行裁剪。
        不拼接半行，不驱逐已有证据，未能提供完整行时返回空列表。
        """
        # 原文以LF定义行；续读首尾的半行不能冒充完整行，否则引用位置会夸大覆盖。
        lines = source_lines(block["text"])
        continuation = block["continuation"]
        if block["truncated"]:
            self.limitations.append("读取有截断；只提供完整行，可按continuation续读。")
        if block["start_column"] > 0:
            if lines:
                lines = lines[1:]
            start = block["start_line"] + 1
        else:
            start = block["start_line"]
        if continuation and continuation["start_column"] > 0 and lines:
            lines = lines[:-1]
        reused, segments = uncovered_segments(
            [
                e
                for e in self.state.evidence
                if (e.repo_id, e.snapshot_id, e.source_version_id)
                == (
                    block.get("repo_id"),
                    block.get("snapshot_id"),
                    block.get("source_version_id"),
                )
            ],
            block["path"],
            start,
            lines,
        )
        ids = reused.copy()
        added = 0
        if reused and not segments:
            parent = next(e for e in self.state.evidence if e.evidence_id == reused[0])
            # A narrower explicit read may create a new, immutable range identity.
            # Original parents stay unchanged; repeated identical fragments reuse IDs.
            if lines and (parent.start_line, parent.end_line) != (start, start + len(lines) - 1) and estimate(parent.model_dump()) > self.limits.active_evidence_bytes // 2:
                fragment = parent.model_copy(update={"evidence_id": f"E{len(self.state.evidence) + 1}", "start_line": start, "end_line": start + len(lines) - 1, "text": "".join(lines)})
                exact = next((e for e in self.state.evidence if e.repo_id == fragment.repo_id and e.path == fragment.path and e.start_line == fragment.start_line and e.end_line == fragment.end_line), None)
                if exact:
                    ids = [exact.evidence_id]
                    self.pinned = ids.copy()
                elif estimate(fragment.model_dump()) <= self.limits.active_evidence_bytes and estimate([e.model_dump() for e in self.state.evidence + [fragment]]) <= self.limits.evidence_estimate:
                    self.state.evidence.append(fragment)
                    ids = [fragment.evidence_id]
                    self.pinned = ids.copy()
                    self.trace.append({"event": "evidence_added", "evidence": fragment.model_dump(), "parent_ids": reused})
        for segment_start, segment_lines in segments:
            original_length = len(segment_lines)
            while segment_lines:
                item = Evidence(
                    evidence_id=f"E{len(self.state.evidence) + 1}",
                    commit=block["commit"],
                    repo_id=block.get("repo_id"),
                    snapshot_id=block.get("snapshot_id"),
                    source_kind=block["source_kind"],
                    source_version_id=block.get("source_version_id"),
                    path=block["path"],
                    start_line=segment_start,
                    end_line=segment_start + len(segment_lines) - 1,
                    text="".join(segment_lines),
                )
                if (
                    estimate([e.model_dump() for e in self.state.evidence + [item]])
                    <= self.limits.evidence_estimate
                ):
                    self.state.evidence.append(item)
                    self.pinned = [item.evidence_id]
                    ids.append(item.evidence_id)
                    added += len(segment_lines)
                    self.trace.append(
                        {"event": "evidence_added", "evidence": item.model_dump()}
                    )
                    break
                segment_lines.pop()
            if len(segment_lines) < original_length:
                self.limitations.append("初始/累计证据受预算限制，部分内容未提供。")
        self.trace.append(
            {
                "event": "evidence_coverage",
                "path": block["path"],
                "read_complete_lines": len(lines),
                "new_lines": added,
                "reused_ids": reused,
                "provided_ids": ids,
            }
        )
        return ids
