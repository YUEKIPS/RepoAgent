"""One publication boundary for validated answers and failed results."""

from repoagent.execution.contracts import Answer, Evidence, Result, origin_fields
from repoagent.execution.notices import build_notices, stop_notice


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
        from repoagent.execution.repair import citation_issues
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
    ):
        published = citations_with_context(answer, evidence)
        if published.citations != answer.citations:
            trace.append(
                {
                    "event": "citation_context_expanded",
                    "original_citations": [c.model_dump() for c in answer.citations],
                    "published_citations": [
                        c.model_dump() for c in published.citations
                    ],
                    "policy": "validated_original_then_full_provided_evidence",
                }
            )
        return Result(
            **facts,
            execution_status="SUCCEEDED",
            answer_status=answer.answer_status,
            answer=answer.answer,
            claims=answer.claims,
            citations=published.citations,
            original_citations=answer.citations,
            limitations=list(dict.fromkeys(limitations + answer.limitations)),
            notices=build_notices(limitations, answer.limitations, observations)
            + (
                [stop_notice(finalization_reason, finalizing=True)]
                if finalization_reason
                else []
            ),
            stop_reason="final_answer",
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
