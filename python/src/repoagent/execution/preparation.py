"""Reusable preparation data, source checks, and initial read windows."""

from pydantic import Field

from repoagent.execution.contracts import (
    Evidence,
    Observation,
    Query,
    StrictModel,
    ToolCall,
    Usage,
)
from repoagent.execution.control import StopExecution
from repoagent.execution.limits import Limits
from repoagent.repositories.sources import descriptor, source_text
from repoagent.tools.store import source_lines


class PreparedContext(StrictModel):
    """本机执行器产物，复用时核对查询、配置及证据原文。"""

    query: Query
    limits: Limits
    model_id: str
    generation_id: str | None = None
    execution_version: str | None = None
    usage: Usage
    trace: list[dict]
    evidence: list[Evidence]
    limitations: list[str]
    preparation_seconds: float = Field(ge=0)
    index_generations: dict = Field(default_factory=dict, exclude_if=lambda v: not v)
    source_failure: str | None = Field(default=None, exclude_if=lambda v: v is None)
    observations: list[Observation] = Field(
        default_factory=list, exclude_if=lambda v: not v
    )


def validate_reuse(
    prepared, *, query, limits, model_id, version, generations, evidence_root,
):
    """Verify a copy before the execution inherits evidence or usage."""
    prepared = PreparedContext.model_validate(prepared.model_dump())
    if (
        prepared.query != query
        or prepared.limits != limits
        or prepared.model_id != model_id
        or prepared.execution_version != version
        or prepared.index_generations != generations()
    ):
        raise ValueError("Prepared context binding/configuration mismatch")
    for evidence in prepared.evidence:
        text = source_text(evidence_root(evidence), evidence.path)
        original = "".join(source_lines(text)[evidence.start_line - 1 : evidence.end_line])
        if evidence.text != original or evidence.commit != descriptor(evidence_root(evidence)).commit:
            raise ValueError("Prepared evidence mismatch")
    return prepared


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


def read_initial_hits(
    hits, search_kind, *, strategy, initial_cursor, state, usage, limits,
    trace, limitations, invoke, observe, advance, source_stopped, stop_retrieval,
):
    """Read candidate windows while the executor owns cursor and checkpoint updates."""
    # 缓存本轮发现的EOF，避免超出文件的窗口被重复读取；最多initial_max_reads次初始读取。
    known_file_ends = {}
    initial_reads = sum(
        observation.call.name == "read_file" and observation.call.call_id.startswith("initial-")
        for observation in state.observations
    )
    for cursor, hit in enumerate(hits):
        if cursor < initial_cursor:
            continue
        start, end = read_window(hit, search_kind, strategy)
        path = hit["path"]
        trace.append(
            {
                "event": "initial_candidate",
                "path": path,
                "start_line": start,
                "end_line": end,
                "strategy": strategy,
            }
        )
        identity = (hit.get("repo_id"), path)
        end = min(end, known_file_ends.get(identity, end))
        if any(
            evidence.repo_id == identity[0]
            and evidence.path == path
            and evidence.start_line <= start
            and end <= evidence.end_line
            for evidence in state.evidence
        ):
            trace.append(
                {
                    "event": "initial_read_covered",
                    "path": path,
                    "start_line": start,
                    "end_line": end,
                }
            )
            continue
        # 初始阶段不耗尽全部工具额度；未读候选在limitations中明确，Agent可继续调查。
        if usage.tool_calls >= limits.max_tool_calls:
            stop_retrieval("tool_calls")
            break
        if initial_reads >= limits.initial_max_reads:
            limitations.append(
                "初始读取达到上限，剩余候选未读取；Agent可在额度内补查。"
            )
            trace.append(
                {
                    "event": "initial_read_limit",
                    "remaining_candidates_not_read": True,
                }
            )
            break
        initial_reads += 1
        call = ToolCall(
            call_id=f"initial-{usage.tool_calls + 1}",
            name="read_file",
            arguments={
                "path": path, "start_line": start, "end_line": end, "repo_id": hit["repo_id"],
            },
        )
        outcome = invoke(call)
        advance(cursor + 1)
        if not outcome.ok:
            if source_stopped():
                if outcome.error.recoverable:
                    observe(call, outcome)
                break
            if outcome.error.recoverable:
                observe(call, outcome)
                continue
            raise StopExecution("FAILED", outcome.error.code)
        block = outcome.result
        if not block["truncated"] and block["end_line"] < end:
            known_file_ends[identity] = block["end_line"]
        observe(call, outcome)
