"""任务级模型会话：容量、次数、取消、一次发送权与实际token统计。"""

import json
import threading
import time
from dataclasses import replace
from uuid import uuid4

from repoagent.execution.contracts import ModelCallRecord, Usage
from repoagent.execution.limits import Limits
from repoagent.execution.meter import RequestMeter
from repoagent.execution.usage_store import UsageStore
from repoagent.models.base import (
    CallOutcome,
    ModelBudgetError,
    ModelCancelledError,
    ModelServiceError,
    ModelTimeoutError,
    _DispatchPermit,
    configuration_id,
)

_DEFAULT = object()


class ModelSession:
    """每任务独立且串行；新真实调用只依赖非金额请求记录，不计算费用。"""

    def __init__(self, adapter, usage_store=None):
        if usage_store is not None and not isinstance(usage_store, UsageStore):
            raise ValueError("A current UsageStore is required")
        self.adapter, self.store = adapter, usage_store
        from repoagent.models.tokenization import counter_for
        self.token_counter, self.meter_identity = counter_for(getattr(adapter, "config", None))
        self.model_id, self.simulated = adapter.model_id, adapter.simulated
        if not self.simulated and not getattr(adapter, "service_identity", None):
            raise ValueError("Live models require a registered service identity")
        self._snapshot_configuration()
        self.task_id = str(uuid4())
        self.usage = Usage(model_id=self.model_id, simulated=self.simulated)
        self.records = []
        self._issued, self._tickets = {}, {}
        self._used = set()
        self._continuations = []
        self.on_delta = None
        self.before_dispatch = None
        self.after_response = None
        self._lock = threading.Lock()
        self._halted = self._bound = False
        self._closed = False
        self._started = time.monotonic()
        self.limits = Limits()
        self.guard = self._default_guard
        self.remaining = lambda: (
            self.limits.deadline_seconds - (time.monotonic() - self._started)
        )
        self.cancelled = lambda: False
        self.max_requests = self.limits.max_model_requests
        self.max_input, self.max_output = (
            self.limits.max_input_estimate,
            self.limits.max_output_estimate,
        )

    def _snapshot_configuration(self):
        self._configuration = json.loads(json.dumps(self.adapter.configuration))
        self._generation_id = configuration_id(self._configuration)

    def _default_guard(self):
        if self.cancelled():
            raise ModelCancelledError(code="cancelled")
        if self.remaining() <= 0:
            raise ModelTimeoutError(code="request_timeout")

    @classmethod
    def for_execution(cls, model, **binding):
        session = (
            model
            if isinstance(model, cls)
            else cls(getattr(model, "adapter", model), getattr(model, "store", None))
        )
        session.bind(**binding)
        return session

    def bind(
        self,
        *,
        task_id,
        usage,
        guard,
        remaining,
        cancelled,
        limits,
        native_thinking="default",
    ):
        """固定任务参数；切换原生选项发生于绑定前，不修改在途适配器。"""
        if self._bound or self.usage.model_requests or self._issued:
            raise ValueError("Model session already belongs to a task")
        if hasattr(self.adapter, "with_native_thinking"):
            self.adapter = self.adapter.with_native_thinking(native_thinking)
            self._snapshot_configuration()
        elif native_thinking != "default":
            raise ModelServiceError(code="unsupported_reasoning_option")
        self._bound = True
        self.task_id, self.usage = task_id, usage
        self.guard, self.remaining, self.cancelled = guard, remaining, cancelled
        self.limits = Limits.model_validate(limits.model_dump())
        self.max_requests = limits.max_model_requests
        self.max_input, self.max_output = (
            limits.max_input_estimate,
            limits.max_output_estimate,
        )
        if hasattr(self.adapter, "begin_transport"):
            self.adapter.begin_transport()

    def close(self):
        self._closed = True
        if hasattr(self.adapter, "close"):
            self.adapter.close()

    @property
    def generation_id(self):
        return self._generation_id

    @property
    def configuration(self):
        return self._configuration | {
            "generation_id": self.generation_id,
            "usage_store": self.store.configuration()
            if self.store
            else "simulated"
            if self.simulated
            else "configuration_required",
        }

    @property
    def format_repair(self):
        return self.adapter.format_repair

    def prepare(self, request, max_output=_DEFAULT):
        """纯本地准备，自动值确实保持None；发行后用完整参数摘要防替换。"""
        self.guard()
        if configuration_id(self.adapter.configuration) != self.generation_id:
            raise ModelServiceError(code="model_configuration_mismatch")
        configured = self.limits.output_for(request.stage)
        limit = configured if max_output is _DEFAULT else max_output
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ModelBudgetError(code="output_tokens")
        if configured is not None and (limit is None or limit > configured):
            raise ModelBudgetError(code="output_tokens")
        if self.simulated:
            prepared = self.adapter.prepare(request, limit)
        else:
            prepared = self.adapter.prepare(
                request, limit, continuation=tuple(self._continuations) if request.stage != "summary" else ()
            )
        if prepared.configuration_id != self.generation_id:
            raise ModelServiceError(code="model_configuration_mismatch")
        capabilities = getattr(getattr(self.adapter, "config", None), "capabilities", None)
        measured = RequestMeter().measure(prepared, capabilities, self.limits, self.token_counter, self.meter_identity)
        prepared = replace(prepared, measurement_json=json.dumps(measured, sort_keys=True).encode())
        self._issued[prepared.call_id] = prepared
        self.guard()
        return prepared

    def discard(self, prepared):
        if (
            self._issued.get(prepared.call_id) != prepared
            or prepared.call_id in self._used
            or prepared.call_id in self._tickets
        ):
            raise ModelBudgetError(code="invalid_prepared_call")
        del self._issued[prepared.call_id]

    def rebase_continuation(self, covered_call_ids):
        """Drop only complete oldest native batches after a durable step summary."""
        covered = set(covered_call_ids)
        kept, last_covered = False, -1
        seen = set()
        for index, raw in enumerate(self._continuations):
            ids = {c["id"] for c in json.loads(raw).get("tool_calls", [])}
            if ids & covered:
                if kept or not ids <= covered:
                    raise ModelServiceError(code="continuation_compaction_unbalanced")
                seen.update(ids)
                last_covered = index
            elif ids:
                kept = True
        if self._continuations and not covered <= seen:
            raise ModelServiceError(code="continuation_compaction_incomplete")
        self._continuations = self._continuations[last_covered + 1:]

    def _validate(self, prepared):
        if (
            self._issued.get(prepared.call_id) != prepared
            or prepared.call_id in self._used
            or prepared.configuration_id != self.generation_id
            or configuration_id(self.adapter.configuration) != self.generation_id
        ):
            raise ModelBudgetError(code="invalid_prepared_call")
        if prepared.input_size > self.max_input:
            raise ModelBudgetError(code="input_estimate")
        if not prepared.measurement.get("hard_fits", True):
            raise ModelBudgetError(code="context_window")
        self.ensure_healthy()
        if self.usage.model_requests >= self.max_requests:
            raise ModelBudgetError(code="model_requests")

    def ensure_healthy(self):
        if self._closed:
            raise ModelServiceError(code="model_session_closed")
        if self._halted:
            raise ModelServiceError(code="model_session_halted")

    def _bind_store(self):
        if self.store is None:
            raise ModelServiceError(code="usage_store_required")
        identity = getattr(self.adapter, "service_identity", None)
        if not identity:
            raise ModelServiceError(code="model_service_identity_required")
        self.store.bind_identity(identity)

    def reserve(self, prepared):
        """仅登记未发送请求/发送票据，无价格和token消费估算。"""
        self._validate(prepared)
        if self.simulated:
            return None
        self._bind_store()
        if prepared.call_id not in self._tickets:
            self._tickets[prepared.call_id] = self.store.reserve(
                self.task_id,
                self.model_id,
                prepared.input_size,
                prepared.max_output,
                prepared.wire_sha256,
            )
        return self._tickets[prepared.call_id]

    def invoke(self, prepared, *, timeout=None, cancelled=None, reservation=None):
        """无SDK自动重试；无论响应是否可解析，都保留请求状态和已知用量。"""
        if not self._lock.acquire(blocking=False):
            raise ModelServiceError(code="concurrent_model_call")
        record = None
        failure = None
        ticket = None
        claimed = False
        claim_failed = False
        state = None
        response = None
        try:
            self._validate(prepared)
            self.guard()
            if reservation is not None and (
                self.simulated or self._tickets.get(prepared.call_id) != reservation
            ):
                raise ModelBudgetError(code="invalid_usage_ticket")
            ticket = self.reserve(prepared)
            if self.before_dispatch:
                self.before_dispatch(prepared, ticket)
            record = ModelCallRecord(
                record_version="model-call-v4-current",
                dispatch_state="not_sent",
                provider=self.adapter.configuration["provider"],
                requested_model=self.model_id,
                wire_sha256=prepared.wire_sha256,
                requested_output_limit=prepared.max_output,
                native_thinking=getattr(self.adapter, "native_thinking", "default"),
            )
            try:
                self.guard()
                cancel = cancelled if cancelled is not None else self.cancelled
                if cancel():
                    raise ModelCancelledError(code="cancelled")
                remaining = min(self.remaining(), self.adapter.request_timeout)
                if timeout is not None:
                    remaining = min(remaining, timeout)
                if remaining <= 0:
                    raise ModelTimeoutError(code="request_timeout")
                if ticket:
                    try:
                        self.store.claim(ticket, self.task_id, prepared.wire_sha256)
                    except Exception:
                        claim_failed = True
                        self._halted = True
                        raise
                    claimed = True
                remaining = min(remaining, self.remaining())
                if cancel():
                    raise ModelCancelledError(code="cancelled")
                if remaining <= 0:
                    raise ModelTimeoutError(code="request_timeout")
                self._used.add(prepared.call_id)
                self.usage.model_requests += 1
                self.usage.input_token_estimate += prepared.measurement["estimated_input_tokens"]
                record.dispatch_state = (
                    "possibly_sent" if not self.simulated else "not_sent"
                )
                stream_options = {"on_delta": self.on_delta} if self.on_delta and not self.simulated else {}
                outcome = self.adapter._send_prepared(
                    prepared,
                    timeout=remaining,
                    cancelled=cancel,
                    permit=_DispatchPermit(prepared),
                    **stream_options,
                )
                record, response, state = (
                    outcome.record,
                    outcome.response,
                    outcome.protocol_state,
                )
            except BaseException as error:
                failure = error
                state = getattr(error, "protocol_state", None)
                if isinstance(error, ModelServiceError) and error.record is not None:
                    record = error.record
                if isinstance(error, ModelServiceError):
                    record.error_code = error.code
                elif claimed and not isinstance(error, (KeyboardInterrupt, SystemExit)):
                    record.error_code = "adapter_internal_error"
                    failure = ModelServiceError(code=record.error_code, record=record)
            record.requested_output_limit = prepared.max_output
            record.output_limit_source = prepared.output_limit_source
            record.native_thinking = getattr(self.adapter, "native_thinking", "default")
            capability = getattr(
                getattr(self.adapter, "config", None), "capabilities", None
            )
            output_bound = (
                prepared.max_output
                if prepared.max_output is not None
                else getattr(capability, "max_output_tokens", None)
            )
            if (
                output_bound is not None
                and record.output_tokens is not None
                and record.output_tokens > output_bound
            ):
                record.error_code = "output_tokens"
                failure = ModelBudgetError(code="output_tokens", record=record)
            if (
                ticket
                and not claim_failed
                and (claimed or record.dispatch_state == "not_sent")
            ):
                try:
                    self.store.settle(
                        ticket, record, claimed and record.dispatch_state != "not_sent"
                    )
                except Exception as error:
                    self._halted = True
                    record.settlement_error = getattr(
                        error, "code", "usage_record_failed"
                    )
                    failure = ModelServiceError(
                        code=record.settlement_error, record=record
                    )
            if not self.simulated:
                self.records.append(record.model_copy(deep=True))
                self.usage.service_calls.append(record.model_copy(deep=True))
                self.usage.reported_input_tokens += record.input_tokens or 0
                self.usage.reported_output_tokens += record.output_tokens or 0
                self.usage.reported_reasoning_tokens += record.reasoning_tokens or 0
                self.usage.requests_without_usage += int(
                    record.input_tokens is None or record.output_tokens is None
                )
            if state is not None and (
                response is None or response.kind in {"tool_call", "tool_calls"}
            ):
                if not isinstance(state, bytes) or len(state) > self.max_input:
                    failure = ModelBudgetError(
                        code="continuation_capacity", record=record
                    )
                else:
                    self._continuations.append(state)
            self.refresh_usage()
            if not self.simulated and record.dispatch_state != "not_sent" and (record.input_tokens is None or record.output_tokens is None):
                self._halted = True
                if failure is None:
                    failure = ModelServiceError(code="model_usage_unknown", record=record)
            if failure:
                if isinstance(failure, ModelServiceError):
                    failure.record = record
                raise failure
            self.usage.output_token_estimate += len(
                json.dumps(response.model_dump(), ensure_ascii=False).encode()
            )
            if self.after_response:
                self.after_response(response)
            self.guard()
            if cancel():
                raise ModelCancelledError(code="cancelled", record=record)
            return CallOutcome(response, record)
        finally:
            self._lock.release()

    def refresh_usage(self):
        """记录读取失败不能伪装成正常统计或成功发布。"""
        if self.store and not self.simulated:
            try:
                self.usage.usage_summary = {
                    "task": self.store.summary(self.task_id),
                    "batch": self.store.summary(),
                }
            except Exception:
                self._halted = True
                self.usage.usage_summary |= {
                    "refresh_error": "usage_summary_unavailable"
                }

    def inherit(self, usage, generation_id):
        if self.usage.model_requests or self.records or self._issued:
            raise ValueError("Cannot reset an active session with prepared usage")
        if generation_id != self.generation_id:
            raise ValueError("Prepared model configuration mismatch")
        if not self.simulated:
            self._bind_store()
            self.store.inherit(self.task_id, usage.service_calls)
        for name in (
            "model_requests",
            "input_token_estimate",
            "output_token_estimate",
            "reported_input_tokens",
            "reported_output_tokens",
            "reported_reasoning_tokens",
            "requests_without_usage",
        ):
            setattr(self.usage, name, getattr(usage, name))
        self.usage.service_calls = [
            r.model_copy(deep=True) for r in usage.service_calls
        ]
        self.refresh_usage()

    def export_state(self):
        return {"generation_id": self.generation_id,
                "meter_identity": self.meter_identity,
                "continuations": [s.decode("utf-8") for s in self._continuations],
                "halted": self._halted}

    def restore_state(self, state):
        if state["generation_id"] != self.generation_id or state["halted"] or state.get("meter_identity") != self.meter_identity:
            raise ModelServiceError(code="checkpoint_configuration_mismatch")
        self._continuations = [s.encode("utf-8") for s in state["continuations"]]
