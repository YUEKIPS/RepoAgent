"""Decisions and answer transformations for the candidate delivery stage."""

from repoagent.execution.delivery.content_review import candidate_revision, review_risks
from repoagent.execution.delivery.repair import (
    PatchValidationError,
    apply_citation_patch,
)


def review_request(answer, state, limits, usage, *, mode, supported, reason, feedback):
    if (
        mode != "agent"
        or not limits.content_review_enabled
        or not supported
        or state.semantic_review_done
        or usage.model_requests >= limits.max_model_requests
    ):
        return None
    risks = review_risks(answer, state, reason, feedback)
    if not risks:
        return None
    candidate = answer.model_dump()
    candidate["citations"] = [
        {key: getattr(citation, key) for key in ("evidence_id", "start_line", "end_line", "claim_ids")}
        for citation in answer.citations
    ]
    return {
        "candidate_revision": candidate_revision(answer),
        "candidate": candidate,
        "risks": risks,
        "review_is_advisory": True,
    }


def check_citation_patch(patch, feedback, evidence):
    try:
        patched = apply_citation_patch(feedback, patch, evidence)
    except PatchValidationError as error:
        return None, {"code": error.code, "citation_index": error.citation_index}
    except (ValueError, KeyError, TypeError):
        return None, {"code": "citation_patch_invalid"}
    return patched, None


def can_follow_up(review, finalization_reason, limits, usage):
    return (
        review.verdict == "needs_evidence"
        and not finalization_reason
        and limits.content_review_followup_models >= 2
        and limits.content_review_followup_tools > 0
        and limits.max_model_requests - usage.model_requests >= 2
        and usage.tool_calls < limits.max_tool_calls
    )


def answer_with_review_warning(candidate, reason, concerns, *, major):
    answer = candidate.model_copy(deep=True)
    labels = {
        "review_unavailable": "本次未取得有效复核结果",
        "local_patch_rejected": "局部修改未通过核对，未应用该修改",
        "review_followup_budget": "补充调查额度已用尽",
        "review_evidence_unavailable": "尚缺关键证据，无法继续补充",
        "review_cannot_apply_locally": "问题超过本次局部修改范围",
        "stale_review_candidate": "复核针对的回答版本已变化",
        "review_interrupted_no_repeat": "复核中断，本次没有重复调用",
    }
    answer.limitations.append(
        "内容复核未完全解决，保留原回答：" + labels.get(reason.split(":", 1)[0], "仍有待核实问题")
    )
    answer.limitations.extend(concerns)
    if major:
        answer.answer_status = "insufficient_evidence"
    return answer
