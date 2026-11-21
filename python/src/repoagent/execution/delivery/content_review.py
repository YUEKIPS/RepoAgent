"""Risk selection and atomic edits; a model review is advisory, never source proof."""

import hashlib
import json
import re

from repoagent.execution.contracts import Answer, materialize_answer
from repoagent.execution.delivery.publication import validate_citations


def candidate_revision(answer):
    return hashlib.sha256(
        json.dumps(
            answer.model_dump(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def review_risks(answer, state, finalization_reason, repair_feedback):
    risks = []
    if answer.answer_status == "answered":
        for objective in state.investigation.objectives:
            if objective.status == "unresolved":
                risks.append(
                    {
                        "kind": "unresolved_objective",
                        "objective_id": objective.objective_id,
                        "question": objective.text,
                    }
                )
        if finalization_reason in {"no_progress", "model_requests", "tool_calls"}:
            risks.append(
                {
                    "kind": "retrieval_stopped_before_complete_answer",
                    "reason": finalization_reason,
                }
            )
    if repair_feedback and repair_feedback.get("mode") != "citation_patch":
        risks.append({"kind": "answer_changed_after_citation_review"})
    return risks[:6]


def apply_review_patch(answer, review, evidence):
    """Validate all edits on an isolated copy, then return it or reject everything."""
    if review.candidate_revision != candidate_revision(answer):
        raise ValueError("stale_review_candidate")
    patch = review.patch
    if patch is None:
        raise ValueError("missing_review_patch")
    body = answer.answer
    spans = []
    for edit in patch.body_edits:
        if body.count(edit.old_text) != 1:
            raise ValueError("ambiguous_body_anchor")
        start = body.index(edit.old_text)
        spans.append((start, start + len(edit.old_text), edit.new_text))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError("overlapping_body_edits")
    changed = sum(end - start for start, end, _ in spans)
    if (
        changed > max(512, len(body) // 2)
        or changed > 3000
        or sum(len(s[2]) for s in spans) > 4000
    ):
        raise ValueError("review_patch_not_local")
    for start, end, text in reversed(spans):
        body = body[:start] + text + body[end:]
    if not body.strip():
        raise ValueError("empty_review_answer")
    updates = {c.claim_id: c for c in patch.claim_updates}
    removed = set(patch.remove_claim_ids)
    if (
        len(updates) != len(patch.claim_updates)
        or len(removed) != len(patch.remove_claim_ids)
        or removed & updates.keys()
    ):
        raise ValueError("duplicate_review_claim_operation")
    existing = {c.claim_id for c in answer.claims}
    if not removed <= existing:
        raise ValueError("unknown_removed_claim")
    claims = [
        updates.pop(c.claim_id, c).model_dump()
        for c in answer.claims
        if c.claim_id not in removed
    ]
    claims += [c.model_dump() for c in updates.values()]
    for identifier in removed:
        if re.search(r"\[" + re.escape(identifier) + r"\]", body):
            raise ValueError("dangling_body_claim")
    refs = [
        c.model_dump(include={"evidence_id", "start_line", "end_line", "claim_ids"})
        for c in answer.citations
    ]
    edits = {c.citation_index: c.citation for c in patch.citation_edits}
    if len(edits) != len(patch.citation_edits) or any(i >= len(refs) for i in edits):
        raise ValueError("invalid_review_citation_index")
    refs = [
        (edits[i].model_dump() if edits[i] is not None else None) if i in edits else ref
        for i, ref in enumerate(refs)
    ]
    refs = [ref for ref in refs if ref is not None] + [
        c.model_dump() for c in patch.citation_additions
    ]
    for ref in refs:
        ref["claim_ids"] = [k for k in ref["claim_ids"] if k not in removed]
    refs = [ref for ref in refs if ref["claim_ids"]]
    wire = dict(
        answer_status=patch.answer_status or answer.answer_status,
        answer=body,
        claims=claims,
        citations=refs,
        limitations=list(answer.limitations) + patch.limitations_additions,
    )
    candidate = materialize_answer(Answer.model_validate(wire), evidence)
    known = {c.claim_id for c in candidate.claims}
    # New dangling labels are rejected; pre-existing formatting is left intact.
    if set(re.findall(r"\[(C[0-9]+)\]", body)) - known:
        raise ValueError("unknown_body_claim")
    validate_citations(candidate, evidence)
    if candidate.model_dump() == answer.model_dump():
        raise ValueError("empty_review_patch")
    return candidate
