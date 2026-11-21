"""One publication boundary for validated answers and failed results."""

from collections import Counter

from repoagent.execution.contracts import (
    Answer,
    Evidence,
    Notice,
    Result,
    materialize_answer,
    origin_fields,
)
from repoagent.execution.delivery.notices import build_notices, stop_notice


class CitationValidationError(ValueError):
    """位置/关联校验错误；仅暴露固定code，避免模型内容随异常泄露。"""

    def __init__(self, code: str):
        """将安全错误码code保存并作为ValueError消息，便于终态分类。"""
        self.code = code
        super().__init__(code)


def validate_citations(answer: Answer, evidence: list[Evidence]) -> None:
    """检查answer中的结论ID和引用是否属于本次evidence。

    返回None表示机器可检查的位置/关联条件通过；失败抛CitationValidationError。
    不分析引用是否支持语义，也不自动修补或删除非法引用。
    """
    answer = materialize_answer(answer, evidence)
    by_id = {item.evidence_id: item for item in evidence}
    claims = {item.claim_id for item in answer.claims}
    if len(claims) != len(answer.claims):
        raise CitationValidationError("duplicate_claim_id")
    if not evidence and answer.claims:
        raise CitationValidationError("claims_without_source_evidence")
    covered = set()
    for citation in answer.citations:
        item = by_id.get(citation.evidence_id)
        if item is None:
            raise CitationValidationError("unknown_evidence_id")
        if origin_fields(citation) != origin_fields(item):
            raise CitationValidationError("citation_source_mismatch")
        if (citation.commit, citation.path, citation.repo_id, citation.snapshot_id) != (
            item.commit,
            item.path,
            item.repo_id,
            item.snapshot_id,
        ):
            raise CitationValidationError("citation_source_mismatch")
        if (
            not item.start_line
            <= citation.start_line
            <= citation.end_line
            <= item.end_line
        ):
            raise CitationValidationError("citation_range_outside_evidence")
        if not set(citation.claim_ids) <= claims:
            raise CitationValidationError("unknown_claim_id")
        covered.update(citation.claim_ids)
    if (answer.answer_status == "answered" and not claims) or covered != claims:
        raise CitationValidationError("answered_claims_without_citations")


def citations_with_context(answer: Answer, evidence: list[Evidence]) -> Answer:
    """验证原引用后，返回展示完整已提供证据范围的Answer副本。

    参数answer和evidence分别为原回答与累计原文；不修改模型原始响应。
    范围扩展是发布策略，不等于模型原始窄引用完整或语义已验证。
    """
    answer = materialize_answer(answer, evidence)
    validate_citations(answer, evidence)
    by_id = {item.evidence_id: item for item in evidence}
    return answer.model_copy(
        update={
            "citations": [
                citation.model_copy(
                    update={
                        "start_line": by_id[citation.evidence_id].start_line,
                        "end_line": by_id[citation.evidence_id].end_line,
                    }
                )
                for citation in answer.citations
            ]
        }
    )


class AnswerPublisher:
    def validate_candidate(self, answer, evidence, all_evidence=()):
        from repoagent.execution.delivery.repair import citation_issues
        return citation_issues(answer, evidence, all_evidence)

    def publish(
        self,
        answer,
        evidence,
        *,
        facts,
        limitations,
        observations,
        trace,
        finalization_reason,
        allow_warnings=False,
        recovery_summary=(),
        delivery_reason=None,
    ):
        answer = materialize_answer(answer, evidence)
        issues = self.validate_candidate(answer, evidence)
        if issues and not allow_warnings:
            raise CitationValidationError(issues[0]["code"])
        by_id = {item.evidence_id: item for item in evidence}
        counts = Counter(c.claim_id for c in answer.claims)
        duplicates = {key for key, count in counts.items() if count > 1}
        invalid = {i["citation_index"] for i in issues if "citation_index" in i}
        invalid.update(i for i, c in enumerate(answer.citations) if duplicates.intersection(c.claim_ids))
        valid = [c for i, c in enumerate(answer.citations) if i not in invalid and c.evidence_id in by_id]
        covered = {key for c in valid for key in c.claim_ids}
        missing = sorted((set(counts) - covered) | duplicates)
        diagnostics, affected = [], set()
        for issue in issues:
            item = dict(issue)
            index = item.get("citation_index")
            claim_ids = answer.citations[index].claim_ids if index is not None else item.get("claim_ids", list(counts))
            item["claim_ids"] = list(claim_ids)
            if index is not None:
                item["original_citation"] = answer.citations[index].model_dump()
            affected.update(claim_ids)
            diagnostics.append(item)
        affected.update(missing)
        notices = build_notices(limitations, answer.limitations, observations)
        if issues:
            notices.append(Notice(source="program", category="evidence", severity="warning",
                                  code="citation_validation_warning",
                                  text="部分引用未通过来源、位置或关联核对。正文已保留，问题引用不能作为已核验依据；请查看对应结论与引用提示。"))
            trace.append({"event": "answer_delivered_with_warnings", "reason": delivery_reason,
                          "issues": diagnostics, "unvalidated_claim_ids": missing})
        status = answer.answer_status
        if missing or (status == "answered" and not counts):
            status = "insufficient_evidence"
        contexts = [by_id[c.evidence_id].model_dump(exclude={"text"}) for c in valid]
        return Result(
            **facts,
            execution_status="SUCCEEDED",
            answer_status=status,
            answer=answer.answer,
            claims=answer.claims,
            citations=valid,
            original_citations=answer.citations,
            citation_contexts=contexts,
            citation_validation="complete" if valid and not issues else "partial" if valid else "none",
            citation_diagnostics=diagnostics,
            affected_claim_ids=sorted(affected),
            unvalidated_claim_ids=missing,
            recovery_summary=list(recovery_summary),
            limitations=list(dict.fromkeys(limitations + answer.limitations)),
            notices=notices
            + (
                [stop_notice(finalization_reason, finalizing=True)]
                if finalization_reason
                else []
            ),
            stop_reason="answer_with_warnings" if issues else "final_answer",
            trace=trace,
        )

    def fail(self, status, reason, *, facts, limitations, observations, trace):
        trace.append({"event": "stopped", "reason": reason})
        return Result(
            **facts,
            execution_status=status,
            original_citations=[],
            limitations=list(dict.fromkeys(limitations)),
            notices=build_notices(limitations, file_issues=observations)
            + [stop_notice(reason)],
            stop_reason=reason,
            trace=trace,
        )
