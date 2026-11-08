"""Citation feedback is execution data; no source access or automatic text edits."""

import hashlib
import json

from repoagent.execution.contracts import origin_fields
from repoagent.tools.store import source_lines


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
        return {
            "candidate": answer.model_dump(), "issues": issues,
            "attempt": self.attempts,
            "allowed_evidence": [e.model_dump(exclude={"text"}) for e in evidence],
            "instruction": "Rewrite the complete answer, claims and citations. Expand/read missing original evidence if tools remain; otherwise withdraw unsupported statements and explain insufficient evidence. Do not change workspace or guess identity. All candidates are validated again.",
        }


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
