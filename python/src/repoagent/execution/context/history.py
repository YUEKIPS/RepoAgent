"""One deterministic conversation selection policy; no implicit model calls."""

from repoagent.execution.contracts import ConversationContext, ConversationTurn


class HistoryPolicy:
    max_turns = None
    max_bytes = None
    input_share = None

    @classmethod
    def budget(cls, limits):
        return limits.max_input_estimate

    @classmethod
    def configuration(cls):
        return dict(
            max_turns=cls.max_turns,
            max_bytes=cls.max_bytes,
            input_share=cls.input_share,
            policy="request-pressure-v2",
            configurable=True,
        )

    @staticmethod
    def select(session, latest, budget, summary=None):
        context = ConversationContext(
            session_id=session["id"],
            workspace_version=session["workspace"]["version"],
            omitted_turns=session["turn_count"],
            summary=summary,
        )
        # 最多读取六轮；不随整个会话的增长加载所有历史/轨迹。
        selected = []
        for job in reversed(latest):
            result = job.get("result") or {}
            turn = ConversationTurn(
                turn_id=job["id"],
                question=job["question"],
                log_text=job.get("log_text"),
                answer=result.get("answer"),
                execution_status=result.get("execution_status") or job["state"],
                answer_status=result.get("answer_status"),
                citation_validation=result.get("citation_validation", "none"),
                unvalidated_claim_ids=result.get("unvalidated_claim_ids", []),
                affected_claim_ids=result.get("affected_claim_ids", []),
                historical_locations=[
                    {
                        k: c[k]
                        for k in (
                            "repo_id",
                            "snapshot_id",
                            "source_kind",
                            "source_version_id",
                            "commit",
                            "path",
                            "start_line",
                            "end_line",
                        )
                        if k in c
                    }
                    for c in result.get("citations", [])
                ],
            )
            trial = [turn] + selected
            selected = trial
        context.turns = selected
        context.omitted_turns -= len(selected)
        return context

    @staticmethod
    def plan_compaction(total, covered, recent_turns):
        return max(covered, total - recent_turns)
