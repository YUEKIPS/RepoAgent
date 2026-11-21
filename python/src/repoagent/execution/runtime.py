"""任务执行核心：基线/Agent共用初始化、证据、请求控制、工具反馈和结果发布。

一次任务先prepare生成或复用初始证据，再单次回答或进入act循环。
模拟与真实协议模型共用边界检查；真实调用另记录服务用量及持久请求状态。
引用位置校验与语义支持是两件事，发布完整上下文不代表模型原引用完全正确。
"""

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from repoagent.execution.context.builder import ContextBuilder
from repoagent.execution.context.evidence import EvidenceManager
from repoagent.execution.context.pressure import PressureCoordinator
from repoagent.execution.context.summary import apply_context_summary
from repoagent.execution.contracts import (
    AgentState,
    Answer,
    Evidence,
    ModelRequest,
    ModelResponse,
    Observation,
    Query,
    Result,
    ToolCall,
    ToolOutcome,
    Usage,
    materialize_answer,
    origin_fields,
)
from repoagent.execution.control import StopExecution, estimate
from repoagent.execution.conversation_session import ConversationSession
from repoagent.execution.delivery.content_review import (
    apply_review_patch,
    candidate_revision,
)
from repoagent.execution.delivery.notices import LIVE_NOTE
from repoagent.execution.delivery.publication import (
    AnswerPublisher,
    CitationValidationError,
)
from repoagent.execution.delivery.publication import (
    citations_with_context as citations_with_context,
)
from repoagent.execution.delivery.publication import (
    validate_citations as validate_citations,
)
from repoagent.execution.delivery.repair import (
    RepairPolicy,
    full_rewrite_feedback,
    is_citation_patch,
)
from repoagent.execution.delivery.workflow import (
    answer_with_review_warning,
    can_follow_up,
    check_citation_patch,
    review_request,
)
from repoagent.execution.journal import RunJournal
from repoagent.execution.limits import Limits
from repoagent.execution.model_session import ModelSession
from repoagent.execution.preparation import (
    PreparedContext,
    read_initial_hits,
    validate_reuse,
)
from repoagent.execution.prompt_spec import FORMAT_GUIDANCE
from repoagent.execution.tool_pipeline import ToolPipeline
from repoagent.models.base import (
    ModelBudgetError,
    ModelCancelledError,
    ModelServiceError,
    ModelTimeoutError,
    ProviderAdapter,
)
from repoagent.repositories.navigation import catalog_hints
from repoagent.repositories.sources import GitHubSource, source_text
from repoagent.repositories.text import TEXT_ERRORS
from repoagent.tools.dispatch import Dispatcher, builtin_registry
from repoagent.tools.schema import SCHEMA_VERSION
from repoagent.tools.store import source_lines
from repoagent.tools.workspace import SOURCE_RESTRICTION_ERRORS, WorkspaceTools

PROTOCOL_VERSION = "controlled-execution-v41-unified-thinking"


class Execution:
    """一次任务的宿主执行器：统一管理状态、工具、请求、预算及最终结果。"""

    def __init__(
        self,
        snapshot: Path,
        query: Query,
        model: ProviderAdapter | ModelSession,
        limits: Limits | None = None,
        cancelled: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        mode: str = "baseline",
        on_progress: Callable[[dict], None] | None = None,
        *,
        checkpoint_sink=None,
        stream_sink=None,
        conversation_session=None,
        summary_sink=None,
        deadline_at=None,
        task_id=None,
        extensions=None,
    ):
        """绑定并校验任务依赖，创建独立状态；此阶段不发模型请求。

        参数snapshot/query指定代码版本，model为模拟或已接入的协议模型，mode选择执行策略。
        limits为空时用默认值；cancelled和clock是可注入回调。
        快照不可查询或版本不匹配会直接抛ValueError，尚未进入run的异常转换。
        """
        # 任务时限从执行器构造开始计算；clock可由测试注入，默认使用单调时钟。
        self.started = clock()
        self.clock, self.cancelled = clock, cancelled
        # 已有模型也重新序列化/校验，防止调用方事后修改对象绕过契约。
        self.query = Query.model_validate(query.model_dump())
        self.limits = Limits.model_validate((limits or Limits()).model_dump())
        self.snapshot, self.model, self.mode = snapshot, model, mode
        # 先验证来源完整性，再绑定Query身份；这不是把原仓库工作区当作当前快照。
        if not isinstance(snapshot, dict):
            raise ValueError("Explicit host repository set required")
        self.tools = WorkspaceTools(snapshot, self.query.workspace)
        first = self.query.workspace.repositories[0].repository
        self.snapshot = snapshot[first]
        self.manifest = self.tools.manifests[first]
        if any(
            m.state not in {"READY", "PARTIAL"} for m in self.tools.manifests.values()
        ):
            raise ValueError("Source is not queryable")
        # 工具持有快照根目录，分发器持有固定函数映射，权限不由模型字符串决定。
        # 可观察任务状态、用量、轨迹分别存放；新任务的列表由default_factory独立创建。
        self.usage = Usage(model_id=model.model_id, simulated=model.simulated)
        self.state = AgentState()
        self.state.investigation.goal = self.query.question
        self.format_repairs = 0
        self.format_repair_scopes = {}
        self.protocol_repairs = {}
        self.isolated_finalization = False
        self.minimal_context = False
        self.last_candidate = None
        self.last_candidate_evidence = []
        self.completed_result = None
        self.recovery_events = []
        self.patch_rejections = 0
        self.source_restrictions = self.tools.restrictions
        self.repair_notice = ""
        self.review_input = None
        self.answer_evidence = None
        self.source_failure = None
        self.finalization_reason = None
        self.trace = [
            {
                "event": "configuration",
                "mode": mode,
                "protocol_version": PROTOCOL_VERSION,
                "tool_schema_version": SCHEMA_VERSION,
                "snapshot_id": query.snapshot_id,
                "commit": query.commit,
                "versions": self.manifest.versions,
                "limits": self.limits.model_dump(),
                "policy": self.query.policy.model_dump(),
                "tool_accounting": "every_attempt_including_search_and_read",
            }
        ]
        self.limitations = (
            ["模拟模型结果，仅验证工程流程；未验证回答质量。"]
            if model.simulated
            else [LIVE_NOTE]
        )
        for name, manifest in self.tools.manifests.items():
            if manifest.state == "PARTIAL":
                self.limitations.append(
                    f"{manifest.source if isinstance(self.tools.roots[name], GitHubSource) else name}: 按当前覆盖检索，未搜到不代表功能不存在。"
                )
        # 任务UUID标识本次运行，与Git commit及snapshot_id的来源身份不同。
        self.task_id = task_id or str(uuid4())
        self.conversation_session = (
            ConversationSession.restore(conversation_session) if conversation_session else
            ConversationSession(query.conversation.session_id if query.conversation else self.task_id, query.workspace.version)
        )
        if self.conversation_session.workspace_version != query.workspace.version or (
            query.conversation and self.conversation_session.session_id != query.conversation.session_id
        ):
            raise ValueError("Conversation session binding mismatch")
        if not self.conversation_session.has_run(self.task_id):
            if not self.conversation_session.surface:
                self.conversation_session.append("system/message", {"run_id": self.task_id, "text": ""}, surface_op="append")
            self.conversation_session.append("run/start", {"run_id": self.task_id})
            self.conversation_session.append("user/message", {
                "run_id": self.task_id, "question": query.question, "log_text": query.log_text,
            }, surface_op="append")
        self.state.bind_session(self.conversation_session, self.task_id)
        self.progress_sink = on_progress
        self.journal = RunJournal(self.task_id, self.trace, self._session_progress)
        for source in self.tools.roots.values():
            if isinstance(source, GitHubSource):
                if source.context is None:
                    raise ValueError("Remote sources need a host-bound run")
                source.context.budget.cancelled = cancelled
                source.context.budget.remaining = self.remaining_seconds
                source.store.begin_run(source.context.owner, self.task_id)
        self.preparation_seconds = 0.0
        self.session = ModelSession.for_execution(
            model,
            native_thinking=query.policy.native_thinking,
            task_id=self.task_id,
            usage=self.usage,
            guard=self.guard,
            remaining=self.remaining_seconds,
            cancelled=self.cancelled,
            limits=self.limits,
        )
        self.trace[0]["model_configuration"] = self.session.configuration
        self.context_builder = ContextBuilder()
        self.evidence_manager = EvidenceManager(
            self.state, self.limits, self.trace, self.limitations
        )
        self.registry = builtin_registry(self.tools, self.evidence_manager)
        self.extensions = extensions
        if extensions:
            extensions.register(self.registry)
        self.registry.seal()
        self.dispatcher = Dispatcher(self.tools, self.registry)
        self.trace[0]["registry_fingerprint"] = self.registry.fingerprint
        self.context_compacting = False
        self.pressure_pending = None
        self.force_compact = False
        self.tool_pipeline = ToolPipeline(
            self.state, self.limits, self.tools, self.evidence_manager, self.limitations, self.registry
        )
        self.publisher = AnswerPublisher()
        self.checkpoint_sink, self.stream_sink = checkpoint_sink, stream_sink
        self.summary_sink = summary_sink
        self.deadline_at = deadline_at
        if self.deadline_at is None and self.limits.deadline_seconds is not None:
            self.deadline_at = time.time() + self.limits.deadline_seconds
        self.repair_policy = RepairPolicy()
        self.repair_feedback = None
        self.candidate_revision = 0
        self.pending_response = None
        self.pending_calls = []
        self.initial_plan = None
        self.initial_hits = None
        self.initial_cursor = 0
        self.initial_complete = False
        self.phase = "preparing"
        self.session.on_delta = self._delta if stream_sink else None
        self.session.before_dispatch = self._before_dispatch
        self.session.after_response = self._after_response
        self.prepared_identity = None

    def _session_progress(self, event):
        kind = {"step_started": "step/start", "step_completed": "step/end", "run_completed": "run/end"}.get(event["event"])
        if kind:
            self.conversation_session.append(kind, {
                "run_id": self.task_id, "step": event.get("step", 0),
                "stage": event.get("stage"), "status": event.get("status"),
            })
        if self.progress_sink:
            self.progress_sink(event)

    def _after_response(self, response):
        from repoagent.execution.contracts import ModelRequest
        self.pending_response = response
        kind = {"plan": "assistant/plan", "summary": "context/summary_response", "citation_patch": "assistant/citation_patch", "review": "context/review_response"}.get(response.kind, "assistant/message")
        self.conversation_session.append(kind, {
            "run_id": self.task_id, "step": self.journal.sequence,
            "response": response.model_dump(),
        }, surface_op="append" if kind == "assistant/message" and response.kind != "citation_patch" else None)
        if self.prepared_identity:
            request = ModelRequest.model_validate_json(self.prepared_identity["request_json"])
            if request.stage in {"act", "answer"} and not is_citation_patch(request.repair_feedback):
                self.evidence_manager.commit_view(e.evidence_id for e in request.evidence)
                self.evidence_manager.decision_completed([e.evidence_id for e in request.evidence] + request.deferred_evidence_ids)
        if response.kind == "plan":
            self.initial_plan = response.plan
        elif response.kind in {"answer", "citation_patch"} and self.prepared_identity:
            request = ModelRequest.model_validate_json(self.prepared_identity["request_json"])
            self.answer_evidence = request.evidence
        self.checkpoint("context_compacting" if self.context_compacting else "response_ready")

    def _before_dispatch(self, prepared, ticket):
        self.prepared_identity = {
            "call_id": prepared.call_id, "wire_sha256": prepared.wire_sha256,
            "configuration_id": prepared.configuration_id,
            "payload": prepared.payload.decode("utf-8"),
            "request_json": prepared.request_json.decode("utf-8"),
            "evidence_manifest": prepared.evidence_manifest,
            "attempt_id": ticket.attempt_id if ticket else None,
            "measurement": prepared.measurement,
            "registry_fingerprint": self.registry.fingerprint,
            "session_revision": self.conversation_session.revision,
        }
        self.conversation_session.append("request/prepared", {
            "run_id": self.task_id, "step": self.journal.sequence,
            "identity": self.prepared_identity,
        })
        payload = json.loads(self.prepared_identity["payload"])
        system = next((m.get("content") for m in payload.get("messages", []) if m.get("role") == "system"), None)
        if isinstance(system, str):
            old = self.conversation_session.current_system()
            if not old or old["data"]["text"] != system:
                self.conversation_session.append("system/message", {
                    "run_id": self.task_id, "step": self.journal.sequence, "text": system,
                }, surface_op={"op": "replace", "start_seq": old["seq"], "end_seq": old["seq"]} if old else "append",
                    source_event_seqs=[old["seq"]] if old else [])
        stage = json.loads(prepared.request_json)["stage"]
        self.checkpoint("context_compacting" if self.context_compacting else "summary_pending" if stage == "summary" else "model_pending")

    def _delta(self, text):
        if self.stream_sink:
            self.stream_sink({"event": "draft_delta", "candidate_revision": self.candidate_revision, "text": text})

    def checkpoint(self, phase):
        self.phase = phase
        self.state.applied_event_seq = self.conversation_session.seq
        self.state.pending_call_ids = [call.call_id for call in self.pending_calls]
        if self.checkpoint_sink:
            self.checkpoint_sink({
                "phase": phase, "execution_version": PROTOCOL_VERSION,
                "registry_fingerprint": self.registry.fingerprint,
                "pressure_pending": self.pressure_pending,
                "meter_identity": self.session.meter_identity,
                "extensions_fingerprint": self.extensions.fingerprint if self.extensions else None,
                "task_id": self.task_id, "query": self.query.model_dump(),
                "limits": self.limits.model_dump(), "mode": self.mode,
                "deadline_at": self.deadline_at, "saved_wall": time.time(),
                "state": self.state.model_dump(), "usage": self.usage.model_dump(),
                "conversation_session": self.conversation_session.export(),
                "trace": self.trace, "limitations": self.limitations,
                "session": self.session.export_state(),
                "source_failure": self.source_failure,
                "finalization_reason": self.finalization_reason,
                "format_repairs": self.format_repairs,
                "format_repair_scopes": self.format_repair_scopes,
                "protocol_repairs": self.protocol_repairs,
                "isolated_finalization": self.isolated_finalization,
                "minimal_context": self.minimal_context,
                "last_candidate": self.last_candidate.model_dump() if self.last_candidate else None,
                "last_candidate_evidence": [e.model_dump() for e in self.last_candidate_evidence],
                "completed_result": self.completed_result.model_dump(exclude={"trace", "usage"}) if self.completed_result else None,
                "recovery_events": self.recovery_events,
                "patch_rejections": self.patch_rejections,
                "source_restrictions": self.source_restrictions,
                "repair_feedback": self.repair_feedback,
                "review_input": self.review_input,
                "repair_seen": self.repair_policy.seen,
                "repair_attempts": self.repair_policy.attempts,
                "candidate_revision": self.candidate_revision,
                "answer_evidence": [e.model_dump() for e in self.answer_evidence] if self.answer_evidence is not None else None,
                "pending_response": self.pending_response.model_dump() if self.pending_response else None,
                "pending_calls": [c.model_dump() for c in self.pending_calls],
                "initial_plan": self.initial_plan.model_dump() if self.initial_plan else None,
                "initial_hits": self.initial_hits, "initial_cursor": self.initial_cursor,
                "initial_complete": self.initial_complete,
                "selection": {"pinned": self.evidence_manager.pinned, "active_ids": self.evidence_manager.active_ids, "cold_ids": self.evidence_manager.cold_ids, "revision": self.evidence_manager.revision},
                "network": self._network_checkpoint(),
                "prepared_identity": self.prepared_identity,
            })

    def _network_checkpoint(self):
        for source in self.tools.roots.values():
            if isinstance(source, GitHubSource):
                context = source.context
                return {"requests": context.budget.requests, "bytes": context.budget.bytes,
                        "seconds": context.budget.seconds, "events": context.budget.events,
                        "new_files": context.new_files, "cache_hits": context.cache_hits}
        return None

    def restore(self, saved):
        from repoagent.execution.contracts import Plan
        if saved.get("meter_identity") != self.session.meter_identity:
            raise ValueError("Checkpoint input meter mismatch")
        if saved.get("registry_fingerprint") != self.registry.fingerprint:
            raise ValueError("Checkpoint tool registry mismatch")
        if saved["phase"] in {"terminal", "execution_failed"}:
            raise ValueError("Execution checkpoint is already terminal")
        if saved["phase"] == "context_compacting" and (not saved.get("pressure_pending") or (saved.get("pending_response") or {}).get("kind") != "summary"):
            raise ValueError("Summary response is unavailable")
        if saved["execution_version"] != PROTOCOL_VERSION or saved["limits"] != self.limits.model_dump() or saved["query"]["workspace"] != self.query.workspace.model_dump():
            raise ValueError("Checkpoint binding mismatch")
        if time.time() < saved["saved_wall"] - 2 or (
            saved["deadline_at"] is not None and time.time() >= saved["deadline_at"]
        ):
            raise ValueError("Checkpoint deadline expired or clock changed")
        self.deadline_at = saved["deadline_at"]
        self.task_id = saved["task_id"]
        self.session.task_id = self.task_id
        self.journal.run_id = self.task_id
        self.preparation_seconds = (
            self.limits.deadline_seconds - max(0, self.deadline_at - time.time())
            if self.limits.deadline_seconds is not None else 0.0
        )
        self.query = Query.model_validate(saved["query"])
        self.prepared_identity = saved.get("prepared_identity")
        if self.prepared_identity:
            from repoagent.models.base import PreparedCall
            from repoagent.models.evidence_view import verify_manifest
            identity = self.prepared_identity
            payload = identity["payload"].encode()
            frozen = PreparedCall(identity["call_id"], identity["configuration_id"], payload,
                                  identity["request_json"].encode(), None, len(payload),
                                  evidence_manifest_json=json.dumps(identity["evidence_manifest"]).encode())
            if frozen.wire_sha256 != identity["wire_sha256"]:
                raise ValueError("Checkpoint prepared payload mismatch")
            verify_manifest(frozen, ModelRequest.model_validate_json(frozen.request_json), simulated=self.session.simulated)
        self.pressure_pending = saved.get("pressure_pending")
        if self.pressure_pending:
            request = json.loads(self.prepared_identity["request_json"])
            field = "summary_input" if self.pressure_pending["kind"] == "history" else "runtime_summary_input"
            if request.get(field) != self.pressure_pending["material"]:
                raise ValueError("Summary checkpoint material mismatch")
        self.state = AgentState.model_validate(saved["state"])
        self.conversation_session = ConversationSession.restore(saved["conversation_session"])
        if self.state.applied_event_seq != self.conversation_session.seq or self.conversation_session.workspace_version != self.query.workspace.version:
            raise ValueError("Checkpoint session watermark mismatch")
        self.state.bind_session(self.conversation_session, self.task_id)
        self.usage = Usage.model_validate(saved["usage"])
        self.session.usage = self.usage
        self.session.restore_state(saved["session"])
        self.trace[:] = saved["trace"]
        self.limitations[:] = saved["limitations"]
        self.journal.sequence = max((t.get("step", 0) for t in self.trace), default=0)
        for name in ("source_failure", "finalization_reason", "format_repairs", "repair_feedback", "review_input", "candidate_revision", "initial_hits", "initial_cursor", "initial_complete", "phase"):
            setattr(self, name, saved[name])
        self.repair_policy.seen = saved["repair_seen"]
        self.repair_policy.attempts = saved["repair_attempts"]
        for name in ("format_repair_scopes", "protocol_repairs", "isolated_finalization", "minimal_context", "recovery_events", "patch_rejections", "source_restrictions"):
            setattr(self, name, saved[name])
        self.tools.restrictions = self.source_restrictions
        self.last_candidate = Answer.model_validate(saved["last_candidate"]) if saved["last_candidate"] else None
        self.last_candidate_evidence = [Evidence.model_validate(e) for e in saved["last_candidate_evidence"]]
        self.completed_result = Result.model_validate(saved["completed_result"] | {"trace": self.trace.copy(), "usage": self.usage}) if saved["completed_result"] else None
        self.pending_response = ModelResponse.model_validate(saved["pending_response"]) if saved["pending_response"] else None
        self.pending_calls = [ToolCall.model_validate(c) for c in saved["pending_calls"]]
        if self.state.pending_call_ids != [call.call_id for call in self.pending_calls]:
            raise ValueError("Checkpoint pending call mismatch")
        self.initial_plan = Plan.model_validate(saved["initial_plan"]) if saved["initial_plan"] else None
        self.answer_evidence = [Evidence.model_validate(e) for e in saved["answer_evidence"]] if saved["answer_evidence"] is not None else None
        self.evidence_manager.state = self.state
        self.tool_pipeline.state = self.state
        for name, value in saved["selection"].items():
            setattr(self.evidence_manager, name, value)
        if saved.get("network"):
            for source in self.tools.roots.values():
                if isinstance(source, GitHubSource):
                    for name, value in saved["network"].items():
                        target = source.context.budget if name in {"requests", "bytes", "seconds", "events"} else source.context
                        setattr(target, name, value)
        for evidence in self.state.evidence:
            original = source_text(self.evidence_root(evidence), evidence.path)
            if "".join(source_lines(original)[evidence.start_line - 1:evidence.end_line]) != evidence.text:
                raise ValueError("Checkpoint source integrity failure")
        self.guard()

    def refresh_usage(self):
        """从独立请求记录刷新本任务/本批次的实际token及完整性；模拟不查询。"""
        self.session.refresh_usage()

    def remaining_seconds(self):
        """None means no task deadline; per-request/network timeouts stay separate."""
        remaining = float("inf")
        if self.limits.deadline_seconds is not None:
            remaining = self.limits.deadline_seconds - (
                self.clock() - self.started + self.preparation_seconds
            )
        if getattr(self, "deadline_at", None) is not None:
            remaining = min(remaining, self.deadline_at - time.time())
        return remaining

    def guard(self):
        """检查取消和累计deadline，触发时抛StopExecution。

        复用准备时也加上已消耗的准备时间，不能通过复用重新获得完整时限。
        """
        if self.cancelled():
            raise StopExecution("CANCELLED", "cancelled")
        if self.remaining_seconds() <= 0:
            raise StopExecution("BUDGET_EXCEEDED", "deadline")

    def stop_retrieval(self, reason: str):
        """只进入一次收尾；不增加调用额度，也不伪造工具观测。"""
        if self.finalization_reason is None:
            self.finalization_reason = reason
            self.trace.append({"event": "finalization", "reason": reason})

    def request(self, stage: str) -> ModelResponse:
        """请求指定stage，真实返回格式不合法时至多纠正一次。

        纠正也走同一个请求入口并计次数和实际用量；其他服务故障直接向外传播。
        只把脱敏字段诊断反馈模型，不回显非法响应内容。
        """
        if stage == "review":
            # One metered attempt; optional review never silently spends another full budget.
            if self.state.review_request_started:
                raise ModelServiceError(code="review_attempt_already_used")
            self.state.review_request_started = True
            self.checkpoint("review_pending")
            return self._request_once(stage)
        scope = f"{stage}:{self.candidate_revision}"
        compacted = False
        try:
            while True:
                try:
                    return self._request_once(stage)
                except ModelServiceError as error:
                    if error.code == "model_context_exceeded" and not compacted:
                        self.session.ensure_healthy()
                        compacted = True
                        self.force_compact = True
                        continue
                    if error.code in {"invalid_model_tool_arguments", "unexpected_model_response"} and stage == "act":
                        if self.recover_protocol(error.code):
                            continue
                    if (error.code != "invalid_model_response" or not self.session.format_repair
                        or self.format_repair_scopes.get(scope, 0)
                        or self.usage.model_requests >= self.limits.max_model_requests):
                        raise
                    try:
                        self.session.ensure_healthy()
                    except ModelServiceError:
                        raise error from None
                    self.format_repairs += 1
                    self.format_repair_scopes[scope] = 1
                    errors = error.record.validation_errors if error.record else []
                    self.repair_notice = (
                        "上一条响应未通过JSON/schema验证：" + ", ".join(errors)
                        + "。" + FORMAT_GUIDANCE
                    )
                    self.trace.append({"event": "format_repair", "stage": stage, "scope": scope,
                                       "attempt": 1, "errors": errors})
                    self.recovery_events.append({"stage": stage, "reason": "invalid_model_response", "action": "format_repair"})
        finally:
            self.repair_notice = ""

    def recover_protocol(self, reason):
        """Reject an entire invalid batch, then request one isolated final answer."""
        if self.protocol_repairs.get(reason) or self.usage.model_requests >= self.limits.max_model_requests:
            return False
        self.session.ensure_healthy()
        self.protocol_repairs[reason] = 1
        self.session.reset_protocol()
        self.pending_calls = []
        self.pending_response = None
        self.isolated_finalization = True
        self.stop_retrieval(reason)
        event = {"stage": "act", "reason": reason, "action": "isolated_final_answer", "tools_executed": False}
        self.recovery_events.append(event)
        self.trace.append({"event": "protocol_recovered", **event})
        self.checkpoint("ready")
        return True

    def _prepare_request_context(self, build):
        pressure = PressureCoordinator(
            task_id=self.task_id, query=self.query, session=self.session,
            history=self.conversation_session, registry=self.registry,
            limits=self.limits, usage=self.usage, trace=self.trace,
            checkpoint=self.checkpoint, request_summary=self._request_context_summary,
            commit_summary=self._commit_context_summary,
        )
        try:
            if self.pressure_pending:
                if not self.pending_response or self.pending_response.kind != "summary":
                    raise ModelServiceError(code="context_summary_response_unavailable")
                self._commit_context_summary(self.pending_response.summary)
            return pressure.prepare(build, force=self.force_compact)
        except (StopExecution, ModelServiceError) as error:
            reason = error.reason if isinstance(error, StopExecution) else error.code
            recoverable = {"context_no_summary_budget", "context_protected_content", "context_summary_input", "context_no_progress", "invalid_summary_provenance"}
            if reason not in recoverable or self.minimal_context or self.repair_feedback:
                raise
            self.guard()
            self.session.ensure_healthy()
            self.session.reset_protocol()
            self.minimal_context = True
            self.isolated_finalization = True
            self.pressure_pending = None
            self.pending_response = None
            self.stop_retrieval(reason)
            self.recovery_events.append({"stage": "context", "reason": reason, "action": "minimal_final_view"})
            built = build(True)
            if built.prepared_call.input_size > self.session.max_input or not built.prepared_call.measurement.get("hard_fits", True):
                self.session.discard(built.prepared_call)
                raise StopExecution("BUDGET_EXCEEDED", "context_protected_content") from None
            self.trace.append({"event": "minimal_final_context", "reason": reason,
                                    "evidence_ids": [e.evidence_id for e in built.message.evidence]})
            self.checkpoint("context_ready")
            return built

    def _commit_context_summary(self, result):
        record, event = apply_context_summary(
            result, pending=self.pressure_pending, query=self.query,
            history=self.conversation_session, task_id=self.task_id,
            session=self.session, registry_fingerprint=self.registry.fingerprint,
        )
        self.trace.append(event)
        self.pending_response = None
        self.pressure_pending = None
        self.checkpoint("context_ready")
        if record and self.summary_sink:
            self.summary_sink(record)

    def _request_context_summary(self, prepared, kind):
        request = ModelRequest.model_validate_json(prepared.request_json)
        material = request.summary_input.model_dump() if kind == "history" else request.runtime_summary_input
        self.pressure_pending = {"kind": kind, "material": material}
        self.context_compacting = True
        self.journal.start("summary")
        self.trace.append({"event": "summary_request_meter", "measurement": prepared.measurement})
        previous = len(self.session.records)
        try:
            response = self.session.invoke(prepared).response
            self.journal.responded("summary")
            self.journal.close("completed")
            return response
        finally:
            self.context_compacting = False
            for record in self.session.records[previous:]:
                self.trace.append({"event": "model_usage", "record": record.model_dump()})
            self.refresh_usage()

    def _request_once(self, stage: str) -> ModelResponse:
        """执行一次受控模型请求并返回重新校验的ModelResponse。

        依据剩余额度决定是否提供工具；裁剪旧观测但保留最新反馈和全部已提供证据。
        真实调用先登记并领取发送权，finally记录失败也可能产生的用量；超限通过停止信号退出。
        """
        self.guard()
        if stage == "act" and self.state.review_followup_models_left == 0:
            raise StopExecution("BUDGET_EXCEEDED", "content_review_followup")
        if self.usage.model_requests >= self.limits.max_model_requests:
            raise StopExecution("BUDGET_EXCEEDED", "model_requests")
        if stage == "act":
            if self.state.review_followup_tools_left == 0:
                self.stop_retrieval("content_review_tools")
            if self.source_failure:
                self.stop_retrieval(self.source_failure)
            elif self.usage.tool_calls >= self.limits.max_tool_calls:
                self.stop_retrieval("tool_calls")
            elif self.limits.max_model_requests - self.usage.model_requests <= max(1, min(self.limits.final_request_reserve, self.limits.max_model_requests // 3)):
                self.stop_retrieval("model_requests")
        def build(pressure):
            return self.context_builder.build(
            registry=self.registry,
            conversation_session=self.conversation_session,
            pressure=pressure,
            stage=stage,
            query=self.query,
            state=self.state,
            usage=self.usage,
            limits=self.limits,
            finalization_reason=self.finalization_reason,
            source_failure=self.source_failure,
            repair_notice=self.repair_notice,
            navigation_hints=catalog_hints(self.tools.roots, self.query.question),
            session=self.session,
            stop_retrieval=self.stop_retrieval,
            trace=self.trace,
            limitations=self.limitations,
            evidence_manager=self.evidence_manager,
            repair_feedback=self.repair_feedback,
            selection_query=self.query.question + " " + " ".join(self.initial_plan.queries if self.initial_plan else []),
            force_no_tools=self.isolated_finalization,
            minimal=self.minimal_context,
            source_restrictions=self.source_restrictions,
            review_input=self.review_input if stage == "review" else None,
            source_capabilities=self.tools.capabilities(),
        )
        built = self._prepare_request_context(build)
        self.force_compact = False
        message, prepared_call = built.message, built.prepared_call
        can_use_tools, preparation_count = built.can_use_tools, built.preparation_count
        output_limit, size = built.output_limit, prepared_call.input_size
        context = message.query.conversation
        self.trace.append(
            {
                "event": "request_context",
                "stage": stage,
                "input_bytes": size,
                "measurement": prepared_call.measurement,
                "registry_fingerprint": self.registry.fingerprint,
                "session_revision": self.conversation_session.revision,
                "output_limit": output_limit,
                "sent_output_limit": prepared_call.max_output,
                "output_limit_source": prepared_call.output_limit_source,
                "output_unit": "json_bytes" if self.model.simulated else "tokens",
                "prepare_count": preparation_count,
                "evidence_ranges": [
                    e.model_dump(exclude={"text"}) for e in message.evidence
                ],
                "sent_evidence_manifest": prepared_call.evidence_manifest,
                "hot_evidence_count": len(message.evidence),
                "cold_evidence_count": len(self.state.evidence) - len(message.evidence),
                "registered_evidence_bytes": estimate([e.model_dump() for e in self.state.evidence]),
                "selection_revision": built.selection_revision,
                "session_event_count": self.conversation_session.seq,
                "session_surface": list(self.conversation_session.surface),
                "source_lines": sum(
                    e.end_line - e.start_line + 1 for e in message.evidence
                ),
                "omitted_observations": message.omitted_observations,
                "conversation": context.model_dump() if context else None,
            }
        )
        previous_calls = self.usage.model_requests
        previous_records = len(self.session.records)
        self.journal.start(stage)
        self.candidate_revision += 1
        if self.stream_sink and stage in {"act", "answer"} and not is_citation_patch(message.repair_feedback):
            self.stream_sink({"event": "draft_start", "candidate_revision": self.candidate_revision})
        self.checkpoint("model_pending")
        try:
            response = self.session.invoke(prepared_call).response
        except Exception as error:
            self.conversation_session.append("assistant/attempt", {
                "run_id": self.task_id, "step": self.journal.sequence,
                "error_code": getattr(error, "code", type(error).__name__),
            })
            self.journal.close("request_failed")
            raise
        finally:
            if self.usage.model_requests > previous_calls:
                self.trace.append(
                    {"event": "model_request", "request": message.model_dump()}
                )
            for record in self.session.records[previous_records:]:
                self.trace.append(
                    {"event": "model_usage", "record": record.model_dump()}
                )
            self.refresh_usage()
            if stage == "act" and self.state.review_followup_models_left is not None:
                self.state.review_followup_models_left = max(0, self.state.review_followup_models_left - (self.usage.model_requests - previous_calls))
        response = ModelResponse.model_validate(response.model_dump())
        self.journal.responded(response.kind)
        size = estimate(response.model_dump())
        self.trace.append(
            {"event": "model_response", "response": response.model_dump()}
        )
        self.guard()
        if self.model.simulated and output_limit is not None and size > output_limit:
            raise StopExecution("BUDGET_EXCEEDED", "output_estimate")
        allowed = {"answer", "tool_call", "tool_calls"} if stage == "act" else {stage}
        if is_citation_patch(message.repair_feedback):
            allowed = {"citation_patch"}
        if response.kind not in allowed:
            raise StopExecution("FAILED", "unexpected_model_response")
        if stage == "act" and not can_use_tools and response.kind not in {"answer", "citation_patch"}:
            reason = "tool_requested_after_finalization"
            if not self.recover_protocol(reason):
                raise StopExecution("FAILED", reason)
            return self.request(stage)
        if response.kind in {"answer", "citation_patch"}:
            self.answer_evidence = [e.model_copy(deep=True) for e in message.evidence]
        # Only an accepted investigation/answer decision consumes the lease.
        # Ending a lease does not cool source; pressure is the only cooler.
        if stage in {"act", "answer"} and not is_citation_patch(message.repair_feedback):
            self.evidence_manager.commit_view(e.evidence_id for e in message.evidence)
            self.evidence_manager.decision_completed([e.evidence_id for e in message.evidence] + message.deferred_evidence_ids)
        self.pending_response = response
        if response.kind == "plan":
            self.initial_plan = response.plan
        self.checkpoint("response_ready")
        return response

    def invoke(self, call: ToolCall) -> ToolOutcome:
        """执行一个ToolCall并返回ToolOutcome，调用前后检查停止条件。

        每次尝试先计数并记录call_id，非法名称/参数也不能免费无限试探。
        """
        self.guard()
        if self.usage.tool_calls >= self.limits.max_tool_calls:
            raise StopExecution("BUDGET_EXCEEDED", "tool_calls")
        # 计的是尝试而不只是成功次数；未知工具和坏参数也占额度，防止无限修正。
        self.usage.tool_calls += 1
        if self.state.review_followup_tools_left is not None:
            self.state.review_followup_tools_left = max(0, self.state.review_followup_tools_left - 1)
        self.conversation_session.append("tool/call", {
            "run_id": self.task_id, "step": self.journal.sequence, "call": call.model_dump(),
        })
        self.trace.append(
            {
                "event": "tool_request",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
            }
        )
        self.checkpoint("tool_inflight")
        prior = next(
            (
                o
                for o in self.state.observations
                if not o.outcome.ok
                and o.outcome.error.code in TEXT_ERRORS
                and o.call.name == call.name == "read_file"
                and o.call.arguments.get("repo_id") == call.arguments.get("repo_id")
                and o.call.arguments.get("path") == call.arguments.get("path")
            ),
            None,
        )
        try:
            cache_key = json.dumps([call.name, self.registry.arguments(call), self.tools.generations()], sort_keys=True, ensure_ascii=False)
        except (KeyError, ValueError):
            cache_key = None
        cacheable = call.name in {"read_file", "get_symbol", "search_code", "list_directory", "outline_file"} and self.registry.spec(call.name).origin == "builtin"
        cached_id = self.state.cached_tool_calls.get(cache_key) if cacheable else None
        cached = next((event["data"]["observation"]["outcome"] for event in reversed(self.conversation_session.events())
                       if event["type"] == "tool/settled" and event["data"]["observation"]["call"]["call_id"] == cached_id), None) if cached_id and call.name != "expand_evidence" else None
        outcome = (prior.outcome.model_copy(update={"call_id": call.call_id}) if prior else
                   ToolOutcome.model_validate(cached).model_copy(update={"call_id": call.call_id}) if cached else self.dispatcher.execute(call))
        if cached:
            self.trace.append({"event": "tool_result_reused", "call_id": call.call_id, "original_call_id": cached_id})
        if prior:
            self.trace.append({"event": "file_error_reused", "call_id": call.call_id})
        self.conversation_session.append("tool/settled", {
            "run_id": self.task_id, "step": self.journal.sequence,
            "observation": Observation(call=call, outcome=outcome).model_dump(),
        })
        if outcome.ok and cache_key is not None and cacheable:
            settled_key = json.dumps([call.name, self.registry.arguments(call), self.tools.generations()], sort_keys=True, ensure_ascii=False)
            self.state.cached_tool_calls[settled_key] = call.call_id
        if not outcome.ok and outcome.error.code in SOURCE_RESTRICTION_ERRORS:
            scope = call.arguments.get("repo_id", "*")
            self.source_restrictions[scope] = outcome.error.code
            self.limitations.append(
                f"公开源码获取受限：{scope} / {outcome.error.code}；该范围停止联网取证，其他允许来源与本地缓存仍可调查。"
            )
            self.trace.append(
                {"event": "source_retrieval_restricted", "scope": scope, "code": outcome.error.code}
            )
        self.trace.append(
            {
                "event": "tool_result",
                "call_id": call.call_id,
                "name": call.name,
                "result": outcome.result,
                "ok": outcome.ok,
                "error": outcome.error.model_dump() if outcome.error else None,
            }
        )
        self.guard()
        return outcome

    def required_tool(self, name: str, params: dict) -> dict | None:
        """调用初始准备必需的工具，返回结果字典。

        参数name/params由宿主生成，ID保留initial-前缀；失败即停止，不伪装成空命中。
        """
        for observation in self.state.observations:
            if observation.call.call_id.startswith("initial-") and observation.call.name == name and observation.call.arguments == params and observation.outcome.ok:
                return observation.outcome.result
        call = ToolCall(
            call_id=f"initial-{self.usage.tool_calls + 1}", name=name, arguments=params
        )
        outcome = self.invoke(call)
        if outcome.ok or outcome.error.recoverable:
            self.observe(call, outcome)
        if not outcome.ok:
            if outcome.error.recoverable:
                return None
            raise StopExecution("FAILED", "validation_or_tool_error")
        return outcome.result

    def add_evidence(self, block: dict) -> list[str]:
        return self.evidence_manager.add(block)

    def evidence_root(self, evidence):

        if evidence.repo_id not in self.tools.roots:
            raise ValueError("Evidence repository is not allowed")
        manifest = self.tools.manifests[evidence.repo_id]
        if origin_fields(evidence) != origin_fields(manifest) or (
            evidence.snapshot_id,
            evidence.commit,
        ) != (
            manifest.snapshot_id,
            manifest.commit,
        ):
            raise ValueError("Evidence version mismatch")
        return self.tools.roots[evidence.repo_id]

    def _advance_initial_cursor(self, cursor):
        self.initial_cursor = cursor

    def prepare(self, prepared: PreparedContext | None = None) -> PreparedContext:
        """生成或核验复用的初始准备，返回PreparedContext。

        prepared为空时规划→搜索→限量读取；否则复验配置/原文，继承计数、耗时及token归因。
        初始读取优先包含声明上下文，限制次数以给Agent追加调查留出空间。
        """
        # 共享准备是宿主产物而非模型输入，必须复核问题/配置和每段原文。
        if prepared is not None:
            prepared = validate_reuse(
                prepared, query=self.query, limits=self.limits, model_id=self.model.model_id,
                version=PROTOCOL_VERSION, generations=self.tools.generations,
                evidence_root=self.evidence_root,
            )
            self.state.evidence = [e.model_copy(deep=True) for e in prepared.evidence]
            self.session.inherit(prepared.usage, prepared.generation_id)
            self.usage.tool_calls = prepared.usage.tool_calls
            self.trace.extend(prepared.trace[1:])
            self.format_repairs = int(
                any(t["event"] == "format_repair" for t in prepared.trace)
            )
            self.limitations.extend(prepared.limitations)
            self.source_failure = prepared.source_failure
            for observation in prepared.observations:
                self.state.record_observation(observation.model_copy(deep=True))
            for entry in prepared.trace:
                if entry["event"] == "finalization":
                    self.stop_retrieval(entry["reason"])
            self.preparation_seconds = prepared.preparation_seconds
            self.guard()
            self.initial_complete = True
            return prepared
        # 首次模型请求只生成字面检索规划，具体搜索与读取仍由程序执行。
        try:
            plan = self.initial_plan or self.request("plan").plan
        except ModelServiceError as error:
            if error.code != "invalid_model_response" or self.mode != "agent":
                raise
            self.session.ensure_healthy()
            self.initial_complete = True
            self.pending_response = None
            self.recovery_events.append({"stage": "plan", "reason": error.code, "action": "continue_investigation"})
            self.journal.close("planning_unavailable")
            self.checkpoint("ready")
            return None
        if self.finalization_reason:
            self.initial_complete = True
            self.pending_response = None
            self.journal.close("retrieval_stopped")
            self.checkpoint("ready")
            return None
        search_kind = plan.kind
        hits = self.initial_hits or self.required_tool(
            "search_code",
            {
                "queries": plan.queries,
                "kind": search_kind,
                "limit": self.limits.top_k,
            },
        )
        if hits is None:
            hits = {"items": [], "truncated": False}
        self.initial_hits = hits
        if hits["truncated"]:
            self.limitations.append("初始检索仅取top-k；更多命中未读取。")
        read_initial_hits(
            hits["items"], search_kind, strategy=self.query.policy.retrieval_strategy,
            initial_cursor=self.initial_cursor, state=self.state, usage=self.usage,
            limits=self.limits, trace=self.trace, limitations=self.limitations,
            invoke=self.invoke, observe=self.observe, advance=self._advance_initial_cursor,
            source_stopped=lambda: bool(self.source_failure), stop_retrieval=self.stop_retrieval,
        )
        self.trace.append(
            {
                "event": "evidence",
                "items": [e.model_dump() for e in self.state.evidence],
            }
        )
        self.journal.close("completed")
        self.initial_complete = True
        self.pending_response = None
        self.checkpoint("ready")
        return PreparedContext(
            query=self.query,
            limits=self.limits,
            model_id=self.model.model_id,
            generation_id=self.session.generation_id,
            execution_version=PROTOCOL_VERSION,
            usage=self.usage.model_copy(deep=True),
            trace=self.trace.copy(),
            evidence=[e.model_copy(deep=True) for e in self.state.evidence],
            limitations=self.limitations.copy(),
            preparation_seconds=max(0.0, self.clock() - self.started),
            index_generations=self.tools.generations(),
            source_failure=self.source_failure,
            observations=[o.model_copy(deep=True) for o in self.state.observations],
        )

    def observe(self, call: ToolCall, outcome: ToolOutcome):
        self.tool_pipeline.observe(call, outcome)
        self.journal.observed(call, outcome)
        if self.pending_calls and self.pending_calls[0].call_id == call.call_id:
            self.pending_calls.pop(0)
        self.pending_response = None
        self.checkpoint("tools_pending" if self.pending_calls else "ready" if self.initial_complete else "preparing")

    def finish(self, answer: Answer, *, allow_warnings=False, delivery_reason=None) -> Result:
        self.guard()
        self.refresh_usage()
        self.session.ensure_healthy()
        self.tools.verify()
        evidence = (
            self.state.evidence
            if self.answer_evidence is None
            else self.answer_evidence
        )
        result = self.publisher.publish(
            answer,
            evidence,
            facts=self._publication_facts(),
            limitations=self.limitations,
            observations=self.state.observations,
            trace=self.trace,
            finalization_reason=self.finalization_reason,
            allow_warnings=allow_warnings,
            recovery_summary=self.recovery_events,
            delivery_reason=delivery_reason,
        )
        self.guard()
        self.journal.finish("SUCCEEDED")
        self.completed_result = result
        self.checkpoint("result_ready")
        result.trace = self.trace.copy()
        return result

    def accept_response(self, response):
        if response.kind != "citation_patch":
            return self.accept_candidate(response.answer)
        evidence = self.answer_evidence
        patched, rejection = check_citation_patch(
            response.citation_patch, self.repair_feedback, evidence,
        )
        if rejection is not None:
            return self.reject_patch(rejection)
        self.pending_response = None
        if patched is None:
            self.trace.append({"event": "citation_patch_fallback", "reason": response.citation_patch.status})
            self.repair_feedback = full_rewrite_feedback(self.repair_feedback)
            if self.usage.model_requests >= self.limits.max_model_requests:
                return self.deliver_candidate("citation_repair_budget")
            self.checkpoint("ready")
            return None
        if self.publisher.validate_candidate(patched, evidence, self.state.evidence):
            return self.reject_patch({"code": "citation_patch_validation_failed"})
        self.trace.append({"event": "citation_patch_applied", "candidate_revision": response.citation_patch.candidate_revision,
                           "citation_indices": [u.citation_index for u in response.citation_patch.citation_updates]})
        self.repair_feedback = None
        return self.accept_candidate(patched)

    def reject_patch(self, details):
        self.pending_response = None
        self.patch_rejections += 1
        self.recovery_events.append({"stage": "citation_patch", "reason": details["code"], "action": "rejected", **details})
        self.trace.append({"event": "citation_patch_rejected", **details})
        if self.patch_rejections <= 1 and self.usage.model_requests < self.limits.max_model_requests:
            self.repair_feedback = self.repair_feedback | {"last_rejection": details}
            self.checkpoint("ready")
            return None
        return self.deliver_candidate("citation_patch_invalid")

    def deliver_candidate(self, reason):
        """Publishing warnings is local; never spends an extra model request."""
        self.guard()
        if self.last_candidate is None:
            raise StopExecution("FAILED", reason)
        self.answer_evidence = [e.model_copy(deep=True) for e in self.last_candidate_evidence]
        self.recovery_events.append({"stage": "publication", "reason": reason, "action": "deliver_with_diagnostics"})
        self.pending_response = None
        return self.finish(self.last_candidate, allow_warnings=True, delivery_reason=reason)

    def deliver_or_fail(self, reason):
        try:
            return self.deliver_candidate(reason)
        except StopExecution as error:
            return self.failed(error.status, error.reason)
        except ModelServiceError as error:
            return self.failed("FAILED", error.code)
        except (ValueError, OSError, sqlite3.Error):
            return self.failed("FAILED", "validation_or_tool_error")

    def accept_candidate(self, answer):
        evidence = self.answer_evidence if self.answer_evidence is not None else self.state.evidence
        answer = materialize_answer(answer, evidence)
        self.last_candidate = answer.model_copy(deep=True)
        self.last_candidate_evidence = [e.model_copy(deep=True) for e in evidence]
        issues = self.publisher.validate_candidate(answer, evidence, self.state.evidence)
        if not issues:
            review_input = review_request(
                answer, self.state, self.limits, self.usage, mode=self.mode,
                supported=getattr(self.session.adapter, "content_review", False),
                reason=self.finalization_reason, feedback=self.repair_feedback,
            )
            if review_input is not None:
                self.state.semantic_review_done = True
                self.evidence_manager.protect(c.evidence_id for c in answer.citations)
                self.review_input = review_input
                self.repair_feedback = None
                self.repair_notice = ""
                self.trace.append({"event": "content_review_triggered", "risks": review_input["risks"],
                                   "thinking": getattr(self.session.adapter, "native_thinking", "default"),
                                   "output_limit": self.limits.content_review_output_tokens})
                self.pending_response = None
                self.checkpoint("ready")
                try:
                    review = self.request("review").review
                except ModelServiceError as error:
                    if error.code in {"immutable_evidence_changed", "evidence_view_mismatch", "evidence_namespace_mismatch"}:
                        raise
                    if error.code not in {"invalid_model_response", "unexpected_model_response", "model_output_truncated",
                                          "unsupported_reasoning_option", "model_context_exceeded", "output_tokens"} and not isinstance(error, ModelBudgetError):
                        raise
                    self.session.ensure_healthy()
                    return self.review_warning("review_unavailable:" + error.code)
                return self.apply_content_review(review)
            return self.finish(answer)
        self.guard()
        if self.usage.model_requests >= self.limits.max_model_requests:
            return self.deliver_candidate("citation_repair_budget")
        feedback = self.repair_policy.next_action(answer, issues, self.state.evidence)
        if feedback is None:
            return self.deliver_candidate("citation_repair_no_progress")
        self.repair_feedback = feedback
        candidate_ids = {c.evidence_id for c in answer.citations}
        self.evidence_manager.protect(e.evidence_id for e in self.state.evidence if e.evidence_id in candidate_ids)
        self.trace.append({"event": "citation_repair", "issues": issues, "attempt": self.repair_policy.attempts})
        self.pending_response = None
        self.checkpoint("ready")
        return None

    def review_warning(self, reason, concerns=(), *, major=False):
        self.review_input = None
        self.pending_response = None
        self.repair_feedback = None
        self.trace.append({"event": "content_review_unresolved", "reason": reason,
                           "concerns": list(concerns), "major": major})
        answer = answer_with_review_warning(self.last_candidate, reason, concerns, major=major)
        self.answer_evidence = self.last_candidate_evidence
        return self.finish(answer)

    def apply_content_review(self, review):
        self.trace.append({"event": "content_review", "verdict": review.verdict,
                           "concerns": review.concerns, "semantic_proof": False})
        self.state.investigation.review_concerns = review.concerns
        self.pending_response = None
        if self.last_candidate is None or review.candidate_revision != candidate_revision(self.last_candidate):
            return self.review_warning("stale_review_candidate")
        if review.verdict == "revise":
            # Auxiliary source projections do not change the investigation's hot/cold view.
            # Patches are nevertheless restricted to what this review actually received.
            sources = ModelRequest.model_validate_json(self.prepared_identity["request_json"]).evidence
            try:
                revised = apply_review_patch(self.last_candidate, review, sources)
            except ValueError as error:
                return self.review_warning("local_patch_rejected:" + str(error), review.concerns,
                                           major=review.severity == "major")
            self.trace.append({"event": "content_review_patch_applied", "candidate_revision": review.candidate_revision})
            self.review_input = None
            self.answer_evidence = sources
            return self.accept_candidate(revised)
        if can_follow_up(review, self.finalization_reason, self.limits, self.usage):
            self.review_input = None
            self.state.review_followup_models_left = self.limits.content_review_followup_models
            self.state.review_followup_tools_left = self.limits.content_review_followup_tools
            self.state.investigation.strategy_hint = "只补读或修订内容复核列出的缺口，保留其他结论：" + "；".join(review.concerns)
            self.checkpoint("ready")
            return None
        if review.verdict == "needs_evidence":
            return self.review_warning("review_evidence_unavailable", review.concerns, major=True)
        if review.verdict == "unresolved":
            return self.review_warning("review_cannot_apply_locally", review.concerns, major=review.severity == "major")
        self.review_input = None
        self.answer_evidence = self.last_candidate_evidence
        return self.finish(self.last_candidate)

    def _publication_facts(self):
        return dict(
            schema_version="result-v3",
            repository_access=self.repository_access(),
            task_id=self.task_id,
            workspace=self.query.workspace,
            coverage_summary={k: m.coverage for k, m in self.tools.manifests.items()},
            usage=self.usage,
            conversation_context=self.context_summary(),
        )

    def failed(self, status: str, reason: str) -> Result:
        self.refresh_usage()
        self.journal.finish(status)
        try:
            self.checkpoint("execution_failed")
        except StopExecution:
            # A storage failure still prevents dispatch; preserve the failure
            # result for the existing database-first publication boundary.
            pass
        return self.publisher.fail(
            status,
            reason,
            facts=self._publication_facts(),
            limitations=self.limitations,
            observations=self.state.observations,
            trace=self.trace,
        )

    def repository_access(self):
        if not self.tools.generations():
            return {}
        source = next(
            s for s in self.tools.roots.values() if isinstance(s, GitHubSource)
        )
        context = source.context
        return {
            "turn_id": context.owner,
            "source_stop_reason": self.source_failure,
            "run_id": self.task_id,
            "new_files": context.new_files,
            "cache_hits": context.cache_hits,
            "requests": context.budget.requests,
            "response_bytes": context.budget.bytes,
            "network_seconds": round(context.budget.seconds, 4),
            "events": context.budget.events,
            "index_generations": self.tools.generations(),
        }

    def context_summary(self):
        if not self.query.conversation:
            return None
        contexts = [
            t["conversation"]
            for t in self.trace
            if t.get("event") == "request_context" and t.get("conversation")
        ]
        current = contexts[-1] if contexts else self.query.conversation.model_dump()
        return {
            "session_id": current["session_id"],
            "workspace_version": current["workspace_version"],
            "included_turn_ids": [t["turn_id"] for t in current["turns"]],
            "omitted_turns": current["omitted_turns"],
            "policy": "request-pressure-v2",
            "summarized_turn_ids": (current.get("summary") or {}).get("source_turn_ids", []),
            "uncovered_turns": max(0, current["omitted_turns"] - len((current.get("summary") or {}).get("source_turn_ids", []))),
            "summary_revision": (current.get("summary") or {}).get("revision"),
        }

    def run(self, prepared: PreparedContext | None = None) -> Result:
        """执行prepare后按模式单次回答或进入显式工具反馈循环。

        批内顺序执行，检查ID和规范化重复签名；可恢复错误反馈，系统错误分类停止。
        返回Result；构造阶段的异常不在本方法try范围内。
        """
        try:
            if self.completed_result is not None:
                self.guard()
                self.session.ensure_healthy()
                self.tools.verify()
                self.trace.append({"event": "result_restored_without_generation"})
                self.completed_result.trace = self.trace.copy()
                return self.completed_result
            if not self.initial_complete:
                if self.mode == "agent" and prepared is None and getattr(self.session.adapter, "native_first", False):
                    self.initial_complete = True
                    self.trace.append({"event": "investigation_started", "bootstrap": "native_tools", "goal": self.query.question})
                    self.checkpoint("ready")
                else:
                    self.prepare(prepared)
            if self.pending_response and self.pending_response.kind == "review":
                result = self.apply_content_review(self.pending_response.review)
                if result:
                    return result
            elif self.review_input is not None and self.last_candidate is not None:
                self.session.ensure_healthy()
                # A started optional review is never dispatched again after interruption.
                return self.review_warning("review_interrupted_no_repeat")
            if self.mode == "baseline":
                if self.finalization_reason == "tool_calls" and not self.state.evidence:
                    raise StopExecution("BUDGET_EXCEEDED", "tool_calls")
                while True:
                    response = self.pending_response if self.pending_response and self.pending_response.kind in {"answer", "citation_patch"} else self.request("answer")
                    result = self.accept_response(response)
                    if result:
                        return result
            # 单Agent反馈循环：请求动作→校验工具→执行并追加观测→下一轮；guard强制有限运行。
            while True:
                if self.pending_calls:
                    response = ModelResponse(kind="tool_calls", tool_calls=self.pending_calls.copy())
                    self.pending_calls = []
                else:
                    response = self.pending_response if self.pending_response and self.pending_response.kind != "plan" else self.request("act")
                if self.source_failure and response.kind not in {"answer", "citation_patch"}:
                    if self.recover_protocol("tool_requested_after_source_stop"):
                        continue
                    raise StopExecution("FAILED", "tool_requested_after_source_stop")
                if response.kind in {"answer", "citation_patch"}:
                    result = self.accept_response(response)
                    if result:
                        return result
                    continue
                calls = (
                    response.tool_calls
                    if response.kind == "tool_calls"
                    else [response.tool_call]
                )
                # 先验证整批调用身份，防止执行一半后才发现重复ID造成观测错配。
                ids = [c.call_id for c in calls]
                if (
                    any(not cid or cid.startswith("initial-") for cid in ids)
                    or len(set(ids)) != len(ids)
                    or set(ids) & set(self.state.call_ids)
                ):
                    if self.recover_protocol("invalid_call_id"):
                        continue
                    raise StopExecution("FAILED", "invalid_call_id")
                allowed_calls = self.limits.max_tool_calls - self.usage.tool_calls
                if self.state.review_followup_tools_left is not None:
                    allowed_calls = min(allowed_calls, self.state.review_followup_tools_left)
                if len(calls) > allowed_calls:
                    self.pending_response = None
                    self.stop_retrieval("tool_calls")
                    continue
                self.pending_calls = calls.copy()
                self.pending_response = None
                self.checkpoint("tools_pending")
                batch_fresh = False
                batch_recovered = []
                for call in calls:
                    try:
                        params = self.registry.arguments(call)
                    except (KeyError, ValueError):
                        params = call.arguments
                    # 用补齐默认值后的参数生成稳定签名；换call_id也不能绕过相同操作检测。
                    signature = json.dumps(
                        [
                            call.name,
                            params,
                            self.tools.generations(),
                        ],
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    seen = signature in self.state.signatures
                    self.state.call_ids.append(call.call_id)
                    if not seen:
                        self.state.signatures.append(signature)
                    self.checkpoint("tool_inflight")
                    before_ids = {e.evidence_id for e in self.evidence_manager.select("")}
                    trace_start = len(self.trace)
                    self.observe(call, self.invoke(call))
                    after_ids = {e.evidence_id for e in self.evidence_manager.select("")}
                    coverage = next((t for t in reversed(self.trace[trace_start:]) if t["event"] == "evidence_coverage"), {})
                    fresh = (coverage.get("new_lines", 0) > 0 if call.name == "read_file" else not seen and call.name not in {"expand_evidence", "update_task"})
                    objective = call.arguments.get("verification_of")
                    verify_key = json.dumps([objective, call.name, params, self.tools.generations()], sort_keys=True)
                    if (objective in {o.objective_id for o in self.state.investigation.objectives}
                            and verify_key not in self.state.verification_attempts):
                        self.state.verification_attempts.append(verify_key)
                        batch_recovered.append("verification:" + verify_key)
                    batch_fresh |= fresh
                    if after_ids - before_ids and not fresh:
                        batch_recovered.append(signature)
                    self.trace.append({"event": "tool_progress", "call_id": call.call_id,
                                       "kind": "new_information" if fresh else "context_restored" if after_ids - before_ids else "unchanged"})
                    if self.source_failure:
                        self.pending_calls = []
                        self.checkpoint("ready")
                        break
                if batch_fresh:
                    self.state.consecutive_no_progress = 0
                    self.state.recovered_signatures = []
                    self.state.strategy_recovery_used = False
                    self.state.investigation.strategy_hint = None
                elif any(s not in self.state.recovered_signatures for s in batch_recovered):
                    self.state.recovered_signatures = list(dict.fromkeys(self.state.recovered_signatures + batch_recovered))
                else:
                    self.state.consecutive_no_progress += 1
                if self.state.consecutive_no_progress >= 2:
                    if not self.state.strategy_recovery_used:
                        self.state.strategy_recovery_used = True
                        self.state.consecutive_no_progress = 0
                        self.state.investigation.strategy_hint = "近期动作未改变来源、覆盖或阅读视图。请明确待核事实，换定位方式／连续结构视图，或交付已支持内容与缺口；不要纯重复。"
                        self.trace.append({"event": "investigation_strategy_recovery", "action": "bounded_strategy_feedback"})
                    else:
                        self.stop_retrieval("no_progress")
                self.checkpoint("ready")
        except StopExecution as error:
            if error.reason == "content_review_followup" and self.last_candidate is not None:
                return self.review_warning("review_followup_budget", self.state.investigation.review_concerns, major=True)
            if error.reason in {"citation_patch_invalid", "citation_repair_no_progress", "citation_repair_budget", "invalid_call_id", "tool_requested_after_finalization", "context_no_summary_budget", "context_protected_content", "context_summary_input", "context_no_progress"} and self.last_candidate is not None:
                return self.deliver_or_fail(error.reason)
            return self.failed(error.status, error.reason)
        except ModelCancelledError:
            return self.failed("CANCELLED", "cancelled")
        except ModelTimeoutError:
            if self.remaining_seconds() <= 0:
                return self.failed("BUDGET_EXCEEDED", "deadline")
            return self.failed("FAILED", "request_timeout")
        except ModelBudgetError as error:
            return self.failed("BUDGET_EXCEEDED", error.code)
        except ModelServiceError as error:
            if error.code in {"invalid_model_response", "invalid_model_tool_arguments", "unexpected_model_response", "invalid_summary_provenance"} and self.last_candidate is not None:
                self.session.ensure_healthy()
                return self.deliver_or_fail(error.code)
            if error.code == "model_output_truncated":
                return self.failed("BUDGET_EXCEEDED", "output_tokens")
            return self.failed("FAILED", error.code)
        except CitationValidationError as error:
            return self.failed("FAILED", error.code)
        except (ValueError, OSError, sqlite3.Error):
            return self.failed("FAILED", "validation_or_tool_error")
        finally:
            self.session.close()
            if self.extensions:
                self.extensions.close()


def prepare_initial(
    snapshot: Path,
    query: Query,
    model: ProviderAdapter | ModelSession,
    limits: Limits | None = None,
) -> PreparedContext:
    """为配对运行生成一份PreparedContext。

    参数与执行器的来源/模型/限制相同；同用量批次可复用真实准备，批次物理请求去重、任务完整归因。
    """
    execution = Execution(snapshot, query, model, limits)
    try:
        return execution.prepare()
    finally:
        execution.session.close()
