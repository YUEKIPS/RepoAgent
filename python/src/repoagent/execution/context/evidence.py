"""完整源码行的覆盖与读取窗口；不推断语义调用关系，也不改变既有证据身份。"""

import re

from repoagent.execution.contracts import Evidence
from repoagent.execution.control import estimate
from repoagent.execution.preparation import read_window as read_window
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


class EvidenceManager:
    """Own cumulative source text; request selection never changes this collection."""

    def __init__(self, state, limits, trace, limitations):
        self.state, self.limits = state, limits
        self.trace, self.limitations = trace, limitations
        self.pinned = []
        self.active_ids = []
        self.cold_ids = []
        self.revision = 0
        self.catalog_next_cursor = None

    def select(self, question):
        """A pure view: cooling is committed only with a completed request."""
        return [e for e in self.state.evidence
                if e.evidence_id not in self.cold_ids or e.evidence_id in self.pinned]

    def commit_view(self, identifiers):
        identifiers = list(identifiers)
        if identifiers != self.active_ids:
            self.revision += 1
        self.active_ids = identifiers
        self.cold_ids = [e.evidence_id for e in self.state.evidence if e.evidence_id not in identifiers]

    def cooling_order(self, question, selected):
        # Dotted/camel identifiers must not be swallowed by adjacent Chinese text.
        identifiers = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", question)
        tokens = {word.lower() for word in identifiers}
        tokens.update(word.lower() for token in identifiers for word in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", token) if len(word) > 2)
        pool = selected
        def score(e):
            return (e.evidence_id in self.pinned,
                    (20 if e.evidence_id in identifiers else 0) + sum(min(len(t), 20) for t in tokens if t in (e.path + " " + e.text).lower()),
                    pool.index(e))
        ranked = sorted(pool, key=score)
        # Preserve each repository's best candidate until the other candidates.
        representatives = {}
        for item in ranked:
            representatives[item.repo_id] = item
        return [e for e in ranked if e != representatives[e.repo_id] and e.evidence_id not in self.pinned] + [e for e in ranked if e == representatives[e.repo_id] and e.evidence_id not in self.pinned]

    def catalog(self, cursor=0, budget=None, active_ids=None):
        if not 0 <= cursor <= len(self.state.evidence):
            raise ValueError("invalid_evidence_cursor")
        budget = budget or self.limits.cold_catalog_bytes
        result = []
        self.catalog_next_cursor = None
        for index in range(cursor, len(self.state.evidence)):
            item = self.state.evidence[index]
            if item.evidence_id in (self.active_ids if active_ids is None else active_ids):
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
        changed = any(i not in {e.evidence_id for e in self.select("")} for i in evidence_ids)
        self.protect(evidence_ids)
        return {"evidence_ids": evidence_ids, "activated": changed,
                "already_visible": not changed, "citable_in_next_request": True,
                "pending_input": True}

    def protect(self, identifiers):
        self.pinned = list(dict.fromkeys(self.pinned + list(identifiers)))

    def decision_completed(self, identifiers):
        self.pinned = [i for i in self.pinned if i not in identifiers]

    def add(self, block: dict) -> list[str]:
        """将read_file返回块加入累计证据，返回可供引用的证据ID列表。

        仅保留完整行；相同范围复用ID，显式较小范围建立不可变子片段。
        累计登记不按输入预算截短，不拼接半行、不驱逐已有证据。
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
        same_source = [
                e
                for e in self.state.evidence
                if (e.repo_id, e.snapshot_id, e.source_version_id)
                == (
                    block.get("repo_id"),
                    block.get("snapshot_id"),
                    block.get("source_version_id"),
                )
            ]
        reused, segments = uncovered_segments(same_source, block["path"], start, lines)
        ids = reused.copy()
        added = 0
        if reused and not segments:
            exact = next((e for e in same_source if e.path == block["path"] and (e.start_line, e.end_line) == (start, start + len(lines) - 1)), None)
            parent = next(e for e in same_source if e.evidence_id == reused[0])
            # A narrower explicit read may create a new, immutable range identity.
            # Original parents stay unchanged; repeated identical fragments reuse IDs.
            if exact:
                ids = [exact.evidence_id]
            elif lines:
                fragment = parent.model_copy(update={"evidence_id": f"E{len(self.state.evidence) + 1}", "start_line": start, "end_line": start + len(lines) - 1, "text": "".join(lines)})
                self.state.evidence.append(fragment)
                ids = [fragment.evidence_id]
                self.trace.append({"event": "evidence_added", "evidence": fragment.model_dump(), "parent_ids": reused,
                                   "representation": "continuous_reading_view"})
        for segment_start, segment_lines in segments:
            if segment_lines:
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
                self.state.evidence.append(item)
                ids.append(item.evidence_id)
                added += len(segment_lines)
                self.trace.append({"event": "evidence_added", "evidence": item.model_dump()})
        self.protect(ids)
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
