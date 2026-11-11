"""Citation feedback is execution data; no source access or automatic text edits."""

import hashlib
import json

from repoagent.execution.contracts import Answer, Citation, CitationPatch, origin_fields
from repoagent.tools.store import source_lines

FULL_REWRITE = "Rewrite the complete answer, claims and citations. Expand/read missing original evidence if tools remain; otherwise withdraw unsupported statements and explain insufficient evidence. Do not change workspace or guess identity. All candidates are validated again."
LOCAL_ISSUES = {"citation_range_outside_evidence", "citation_source_mismatch"}


def is_citation_patch(feedback):
    return bool(feedback and feedback.get("mode") == "citation_patch")


def candidate_revision(answer):
    payload = json.dumps(answer.model_dump(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def full_rewrite_feedback(feedback):
    return {
        k: v for k, v in feedback.items()
        if k not in {"mode", "candidate_revision"}
    } | {"instruction": FULL_REWRITE}


def apply_citation_patch(feedback, patch: CitationPatch, evidence):
    """只在候选副本替换获准引用；全量校验由发布入口继续执行。"""
    original = Answer.model_validate(feedback["candidate"])
    if (
        not is_citation_patch(feedback)
        or patch.candidate_revision != feedback["candidate_revision"]
        or candidate_revision(original) != patch.candidate_revision
    ):
        raise ValueError("citation_patch_candidate_mismatch")
    if patch.status != "patch":
        return None
    allowed = {i["citation_index"] for i in feedback["issues"]}
    updates = {u.citation_index: u for u in patch.citation_updates}
    if len(updates) != len(patch.citation_updates) or set(updates) != allowed:
        raise ValueError("citation_patch_indices")
    by_id = {e.evidence_id: e for e in evidence}
    replacements = {}
    for index, update in updates.items():
        if index >= len(original.citations):
            raise ValueError("citation_patch_indices")
        old = original.citations[index]
        # 绑定原证据而非候选里可能错误的来源声明，禁止改引其他文件/版本。
        anchor = by_id.get(old.evidence_id)
        if anchor is None:
            raise ValueError("citation_patch_missing_evidence")
        references = []
        for ref in update.replacement:
            item = by_id.get(ref.evidence_id)
            if (
                item is None
                or origin_fields(item) != origin_fields(anchor)
                or (item.repo_id, item.commit, item.path)
                != (anchor.repo_id, anchor.commit, anchor.path)
            ):
                raise ValueError("citation_patch_source_mismatch")
            if not item.start_line <= ref.start_line <= ref.end_line <= item.end_line:
                raise ValueError("citation_patch_range")
            references.append(Citation(
                **item.model_dump(exclude={"text", "start_line", "end_line"}),
                start_line=ref.start_line, end_line=ref.end_line,
                claim_ids=old.claim_ids.copy(),
            ))
        replacements[index] = references
    # 全部索引指向原候选，一次构造避免前面的拆分导致后续引用索引偏移。
    citations = [
        c for i, old in enumerate(original.citations)
        for c in replacements.get(i, [old.model_copy(deep=True)])
    ]
    return original.model_copy(deep=True, update={"citations": citations})


class RepairPolicy:
    def __init__(self):
        self.seen = []
        self.attempts = 0

    def next_action(self, answer, issues, evidence):
        # Ignore model-assigned IDs and prose changes when identifying stagnation.
        # Narrow aliases and hot/cold changes never become newly acquired facts.
        lines = {}
        for item in evidence:
            identity = (item.repo_id, item.source_version_id or item.snapshot_id, item.commit, item.path)
            for offset, line in enumerate(source_lines(item.text)):
                lines[(*identity, item.start_line + offset)] = line
        basis = sorted((key, hashlib.sha256(value.encode()).hexdigest()) for key, value in lines.items())
        problems = sorted(
            (i["code"], i.get("expected_id", "") if i["code"] != "unknown_evidence_id" else "unknown", i.get("expected_range", ""))
            for i in issues
        )
        fingerprint = hashlib.sha256(json.dumps([problems, basis], sort_keys=True).encode()).hexdigest()
        if fingerprint in self.seen:
            return None
        self.seen.append(fingerprint)
        self.attempts += 1
        feedback = {
            "candidate": answer.model_dump(), "issues": issues,
            "attempt": self.attempts,
            "allowed_evidence": [e.model_dump(exclude={"text"}) for e in evidence],
            "instruction": FULL_REWRITE,
        }
        if issues and all(i["code"] in LOCAL_ISSUES for i in issues):
            feedback.update(mode="citation_patch", candidate_revision=candidate_revision(answer))
            feedback["instruction"] = (
                "Return a CitationPatch JSON object, not a rewritten answer. Only replace the indicated original citation indices. "
                "Preserve all answer text, claims and claim associations. Each replacement must cite actually provided original lines supporting the unchanged claim. "
                "Split cross-fragment ranges into separate evidence IDs; never silently clip a range to hide missing evidence. "
                "Use needs_evidence if original source is missing, or needs_answer_revision if the claim/answer must change. "
                "For those statuses return no updates. Do not invent evidence or identity."
            )
        return feedback


def citation_issues(answer, evidence, all_evidence=()):
    active = {e.evidence_id: e for e in evidence}
    known = {e.evidence_id: e for e in all_evidence}
    issues = []
    ids = {c.claim_id for c in answer.claims}
    if len(ids) != len(answer.claims):
        issues.append({"code": "duplicate_claim_id"})
    covered = set()
    for index, citation in enumerate(answer.citations):
        item = active.get(citation.evidence_id)
        issue = {"citation_index": index, "expected_id": citation.evidence_id}
        if item is None:
            issues.append(issue | {"code": "evidence_not_in_request" if citation.evidence_id in known else "unknown_evidence_id"})
            continue
        if origin_fields(citation) != origin_fields(item) or any(
            getattr(citation, k) != getattr(item, k) for k in ("repo_id", "commit", "path")
        ):
            issues.append(issue | {"code": "citation_source_mismatch", "allowed": item.model_dump(exclude={"text"})})
        if not item.start_line <= citation.start_line <= citation.end_line <= item.end_line:
            issues.append(issue | {"code": "citation_range_outside_evidence", "expected_range": f"{item.start_line}:{item.end_line}"})
        if not set(citation.claim_ids) <= ids:
            issues.append(issue | {"code": "unknown_claim_id"})
        covered.update(citation.claim_ids)
    if not evidence and answer.claims:
        issues.append({"code": "claims_without_source_evidence"})
    if (answer.answer_status == "answered" and not ids) or covered != ids:
        issues.append({"code": "answered_claims_without_citations"})
    return issues[:40]
