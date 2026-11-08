"""运行时数据契约：使用Pydantic限定问题、证据、调用、用量和终态。

字段注解不仅供IDE阅读，也参与运行时校验；extra=forbid拒绝未声明字段。
教学说明使用普通注释补充，避免类docstring变化进入JSON schema并改变模型输入。
"""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

COMMIT = r"^[0-9a-f]{40}$"


# 全部运行时契约的基类：严格类型校验并拒绝额外字段。
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExecutionPolicy(StrictModel):
    # 风格和可复现实验策略属于宿主任务配置，不由源码或工具结果改写。
    answer_style: Literal["auto", "brief", "detailed"] = "auto"
    prompt_profile: Literal["structured"] = "structured"
    retrieval_strategy: Literal["standard"] = "standard"
    native_thinking: str = Field(
        default="default", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$"
    )


class ConversationTurn(StrictModel):
    turn_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    question: str
    log_text: str | None = None
    answer: str | None = None
    execution_status: str
    answer_status: str | None = None
    # 仅为重新查证的定位线索，不能作为本轮可引用Evidence。
    historical_locations: list[dict] = Field(default_factory=list)


class SourceReference(StrictModel):
    source_kind: Literal["local_snapshot", "github_lazy"]
    snapshot_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    source_version_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def source_identity(self):
        if self.source_kind == "github_lazy":
            if not self.source_version_id or self.snapshot_id is not None:
                raise ValueError("Remote source requires its own version identity")
        elif not self.snapshot_id or self.source_version_id is not None:
            raise ValueError("Local source requires snapshot identity only")
        return self


def origin_fields(value):
    kind = value["source_kind"] if isinstance(value, dict) else value.source_kind
    key = "source_version_id" if kind == "github_lazy" else "snapshot_id"
    identity = value[key] if isinstance(value, dict) else getattr(value, key)
    return {"source_kind": kind, key: identity}


class RepositoryBinding(SourceReference):
    repository: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    commit: str = Field(pattern=COMMIT)

    @model_validator(mode="after")
    def required_origin(self):
        if not self.snapshot_id and not self.source_version_id:
            raise ValueError("Missing source identity")
        return self


class Workspace(StrictModel):
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    repositories: list[RepositoryBinding] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def binding(self):
        names = [r.repository for r in self.repositories]
        if names != sorted(set(names)):
            raise ValueError("Workspace repositories must be unique and sorted")
        payload = json.dumps(
            [r.model_dump() for r in self.repositories],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
        if self.version != hashlib.sha256(payload).hexdigest():
            raise ValueError("Workspace version mismatch")
        return self

    @classmethod
    def bind(cls, manifests):
        members = [
            dict(repository=name, commit=m.commit, **origin_fields(m))
            for name, m in sorted(manifests.items())
        ]
        payload = json.dumps(members, ensure_ascii=False, sort_keys=True).encode()
        return cls(version=hashlib.sha256(payload).hexdigest(), repositories=members)


class SummaryItem(StrictModel):
    kind: Literal["goal", "correction", "reference", "unresolved", "location"]
    text: str = Field(min_length=1, max_length=2000)
    turn_ids: list[str] = Field(min_length=1, max_length=100)


class SummaryResult(StrictModel):
    items: list[SummaryItem] = Field(default_factory=list, max_length=40)


class ConversationSummary(StrictModel):
    revision: str
    session_id: str
    workspace_version: str
    covered_through_sequence: int = Field(ge=1)
    source_turn_ids: list[str]
    source_hash: str
    generation_id: str
    prompt_version: str = "history-summary-v1"
    result: SummaryResult


class SummaryTurn(ConversationTurn):
    execution_status: str = "unknown"


class SummaryRequest(StrictModel):
    turns: list[SummaryTurn] = Field(min_length=1, max_length=64)
    previous_summary: ConversationSummary | None = None
    source_turn_ids: list[str] = Field(min_length=1)
    covered_through_sequence: int = Field(ge=1)
    base_revision: str | None = None
    session_messages: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def source_binding(self):
        if len(set(self.source_turn_ids)) != len(self.source_turn_ids) or not {t.turn_id for t in self.turns} <= set(self.source_turn_ids):
            raise ValueError("Invalid summary input provenance")
        return self


class ConversationContext(StrictModel):
    schema_version: Literal["conversation-v3"] = "conversation-v3"
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workspace_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    turns: list[ConversationTurn] = Field(default_factory=list)
    omitted_turns: int = Field(default=0, ge=0)
    summary: ConversationSummary | None = None

    @model_validator(mode="after")
    def summary_binding(self):
        if self.summary and (self.summary.session_id, self.summary.workspace_version) != (self.session_id, self.workspace_version):
            raise ValueError("Summary conversation binding mismatch")
        return self

class Query(StrictModel):
    workspace: Workspace
    question: str = Field(min_length=1, max_length=4000)
    log_text: str | None = Field(default=None, max_length=12000)
    policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    conversation: ConversationContext | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def binding(self):
        if not self.question.strip():
            raise ValueError("Question must not be blank")
        if self.conversation and self.conversation.workspace_version != self.workspace.version:
            raise ValueError("Conversation workspace binding mismatch")
        return self

    @property
    def commit(self):
        return self.workspace.repositories[0].commit

    @property
    def snapshot_id(self):
        return self.workspace.repositories[0].snapshot_id

    @property
    def source_kind(self):
        return self.workspace.repositories[0].source_kind

    @property
    def source_version_id(self):
        return self.workspace.repositories[0].source_version_id


# 初始检索规划：1～3个字面词共用一种text/path/symbol检索类型。
class Plan(StrictModel):
    queries: list[str] = Field(min_length=1, max_length=3)
    kind: Literal["text", "path", "symbol"] = "symbol"

    @model_validator(mode="after")
    def valid_queries(self):
        """检查每个检索词非空且不超过512字符；通过后返回self。"""
        if any(not q.strip() or len(q) > 512 for q in self.queries):
            raise ValueError("Invalid search query")
        return self


# 实际提供给模型的原文证据：版本、路径、闭区间行号、文本和证据ID。
class Evidence(SourceReference):
    evidence_id: str
    repo_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    commit: str = Field(pattern=COMMIT)
    path: str
    # 外部引用统一1-based闭区间；text应对应这个范围内实际提供的完整行。
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str

    @model_validator(mode="after")
    def ordered(self):
        """拒绝起止行倒置；只检查范围顺序，原文真实性由快照和执行层验证。"""
        if self.end_line < self.start_line:
            raise ValueError("Reversed evidence range")
        return self


# 模型引用：evidence_id指向来源，claim_ids将该范围关联到结构化结论。
class Citation(SourceReference):
    evidence_id: str
    repo_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    commit: str = Field(pattern=COMMIT)
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    # 一个引用可支撑多个结构化结论；程序检查关联存在，不判断支持语义。
    claim_ids: list[str] = Field(min_length=1)


# 单条结构化结论；claim_id用于引用关联，不等于Java符号ID。
class Claim(StrictModel):
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


# 最终作答契约：answered或insufficient_evidence，以及结论、引用与限制。
class Answer(StrictModel):
    answer_status: Literal["answered", "insufficient_evidence"]
    answer: str = Field(min_length=1)
    # default_factory为每个实例新建列表；不共享不同任务的可变容器。
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


# 工具请求契约：名称、参数和调用ID；宿主仍需白名单及参数模型校验。
class ToolCall(StrictModel):
    """工具调用必须带显式关联身份。"""

    call_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    arguments: dict


# 脱敏工具错误：固定错误码和是否允许在预算内修改参数重试。
class ToolError(StrictModel):
    code: str
    recoverable: bool


# 工具返回契约：成功只携带result，失败只携带error，并保留调用身份。
class ToolOutcome(StrictModel):
    call_id: str
    name: str
    ok: bool
    result: dict | None = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def consistent(self):
        """检查成功/失败标记与result、error互斥关系；不允许矛盾结果进入循环。"""
        if self.ok != (self.result is not None and self.error is None):
            raise ValueError("Inconsistent tool outcome")
        if not self.ok and (self.error is None or self.result is not None):
            raise ValueError("Failed tool outcome requires an error")
        return self


# 一次工具请求与结果组成的观测；整对保存，裁剪时也整对移除。
class Observation(StrictModel):
    call: ToolCall
    outcome: ToolOutcome

    @model_validator(mode="after")
    def correlated(self):
        """核对call_id与工具名，防止将别次工具结果误配到当前请求。"""
        if (self.call.call_id, self.call.name) != (
            self.outcome.call_id,
            self.outcome.name,
        ):
            raise ValueError("Tool call/result mismatch")
        return self


# 每次请求的显式上下文：阶段、指令、当前问题、累计证据、工具和观测。
class ModelRequest(StrictModel):
    navigation_hints: list[dict] = Field(
        default_factory=list, exclude_if=lambda v: not v
    )
    stage: Literal["plan", "answer", "act", "summary"]
    instruction: str
    query: Query
    evidence: list[Evidence] = Field(default_factory=list)
    available_tools: list[dict] = Field(default_factory=list)
    # 工具观测是数据，不拥有修改系统权限的效力。
    observations: list[Observation] = Field(default_factory=list)
    omitted_observations: int = Field(default=0, ge=0)
    # 包含本次请求在内；工具之后还需一次模型调用来形成最终答案。
    remaining_model_requests: int | None = Field(default=None, ge=0)
    remaining_tool_calls: int | None = Field(default=None, ge=0)
    cold_evidence: list[dict] = Field(default_factory=list)
    cold_evidence_next_cursor: int | None = Field(default=None, ge=0)
    repair_feedback: dict | None = None
    summary_input: SummaryRequest | None = None
    stream: bool = False
    observation_notes: list[dict] = Field(default_factory=list)
    runtime_memory: dict | None = None
    runtime_summary_input: dict | None = None
    session_messages: list[dict] = Field(default_factory=list)


# 统一内部响应：规划、最终回答、单工具或批工具，kind决定唯一载荷。
class ModelResponse(StrictModel):
    kind: Literal["plan", "answer", "tool_call", "tool_calls", "summary"]
    plan: Plan | None = None
    answer: Answer | None = None
    tool_call: ToolCall | None = None
    tool_calls: list[ToolCall] | None = Field(default=None, min_length=1, max_length=8)
    summary: SummaryResult | None = None

    @model_validator(mode="after")
    def matching_payload(self):
        """根据kind检查恰好一个对应载荷存在；拒绝混合或缺失的响应。"""
        for name in ("plan", "answer", "tool_call", "tool_calls", "summary"):
            if (getattr(self, name) is not None) != (self.kind == name):
                raise ValueError("Response kind and payload do not match")
        return self


# 单任务的累计证据、工具观测、调用ID和重复检测签名；不拼接跨任务历史。
class AgentState(StrictModel):
    """Current work state; tool observations are derived from Session events."""

    evidence: list[Evidence] = Field(default_factory=list)
    call_ids: list[str] = Field(default_factory=list)
    signatures: list[str] = Field(default_factory=list)
    session_id: str | None = None
    run_id: str | None = None
    applied_event_seq: int = Field(default=0, ge=0)
    pending_call_ids: list[str] = Field(default_factory=list)
    _conversation_session: object = PrivateAttr(default=None)

    def bind_session(self, session, run_id):
        self._conversation_session = session
        self.session_id, self.run_id = session.session_id, run_id
        self.applied_event_seq = session.seq

    @property
    def observations(self):
        if self._conversation_session is None:
            return []
        return self._conversation_session.observations(self.run_id)

    @property
    def evidence_refs(self):
        return [e.evidence_id for e in self.evidence]

    def record_observation(self, observation, raw_outcome=None, step=0):
        if self._conversation_session is None:
            from repoagent.execution.conversation_session import ConversationSession
            self.bind_session(ConversationSession("detached", "unbound"), "detached")
        if self._conversation_session.has_settled_tool(self.run_id, observation.call.call_id):
            raw_outcome = None
        self._conversation_session.record_observation(
            self.run_id, observation, raw_outcome=raw_outcome, step=step
        )
        self.applied_event_seq = self._conversation_session.seq


# 单次真实请求记录：服务身份、用量、响应形状、脱敏错误和发送状态。
class ModelCallRecord(StrictModel):
    record_version: Literal["model-call-v4-current"] = "model-call-v4-current"
    dispatch_state: Literal[
        "unknown", "not_sent", "possibly_sent", "response_received"
    ] = "unknown"
    settlement_error: str | None = None
    provider: str
    requested_model: str
    returned_model: str | None = None
    request_id: str | None = None
    response_id: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_hit_input_tokens: int | None = Field(default=None, ge=0)
    cache_miss_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    requested_output_limit: int | None = Field(default=None, ge=1)
    output_limit_source: str | None = None
    native_thinking: str | None = None
    usage_attempt_id: str | None = None
    usage_batch_id: str | None = None
    usage_status: Literal["reported", "unknown", "not_sent", "duplicate"] | None = None
    finish_reason: str | None = None
    response_shape: Literal["plan_object", "answer_object", "summary_object"] | None = None
    error_code: str | None = None
    # 仅安全的异常类别，不保存异常文字、代理地址、请求头或凭据。
    transport_failure: (
        Literal[
            "dns",
            "tls",
            "proxy",
            "connect",
            "read",
            "write",
            "remote_protocol",
            "connect_timeout",
            "read_timeout",
            "write_timeout",
            "pool_timeout",
            "runtime",
            "unknown",
        ]
        | None
    ) = None
    validation_errors: list[str] = Field(default_factory=list)
    # 记录载荷摘要而非密钥，供物理请求去重与复查。
    wire_sha256: str | None = None
    # 未知用量与未发送不同，不能把缺usage当作零费用。
    billing_status: Literal[
        "not_calculated", "unknown", "not_sent"
    ] = "not_calculated"


# 任务用量汇总：区分字节估算、服务报告token、未知用量请求。
class Usage(StrictModel):
    model_id: str
    simulated: bool = True
    model_requests: int = 0
    tool_calls: int = 0
    input_token_estimate: int = 0
    output_token_estimate: int = 0
    estimation_method: str = "utf8_bytes_upper_estimate_not_billing"
    # 供应商报告token与发送前完整请求token估算分开保存。
    reported_input_tokens: int = 0
    reported_output_tokens: int = 0
    requests_without_usage: int = 0
    service_calls: list[ModelCallRecord] = Field(default_factory=list)
    usage_summary: dict = Field(default_factory=dict)
    reported_reasoning_tokens: int = 0


# 最终交付模型：执行状态与回答状态分开，覆盖、用量和事件轨迹始终可追踪。
class Notice(StrictModel):
    source: Literal["program", "model"]
    category: Literal["evaluation", "billing", "execution", "evidence", "answer"]
    severity: Literal["info", "warning"]
    code: str
    text: str


class Result(StrictModel):
    schema_version: Literal["result-v2"]
    repository_access: dict = Field(default_factory=dict, exclude_if=lambda v: not v)
    task_id: str
    workspace: Workspace
    # 执行成功不等于回答语义正确；正常证据不足也是一种成功完成。
    execution_status: Literal["SUCCEEDED", "FAILED", "BUDGET_EXCEEDED", "CANCELLED"]
    answer_status: Literal["answered", "insufficient_evidence"] | None = None
    answer: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    original_citations: list[Citation]
    limitations: list[str] = Field(default_factory=list)
    notices: list[Notice]
    coverage_summary: dict
    usage: Usage
    stop_reason: str
    trace: list[dict]
    conversation_context: dict | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def consistent_status(self):
        """成功必须有回答状态及文本；失败不能发布最终答案、结论或引用。"""
        success = self.execution_status == "SUCCEEDED"
        if success != (self.answer_status is not None and self.answer is not None):
            raise ValueError("Inconsistent execution and answer status")
        if not success and (
            self.claims
            or self.citations
            or self.original_citations
            or self.answer is not None
        ):
            raise ValueError("Failed execution cannot publish a final answer")
        if not success and self.answer_status is not None:
            raise ValueError("Failed execution cannot have answer status")
        return self
