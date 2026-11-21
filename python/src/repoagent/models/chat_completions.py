"""已配置服务薄适配层：构造消息，调用兼容SDK，校验并转换响应。

OpenAI SDK在此连接已配置服务端点，不代表使用OpenAI模型或计费账户。
请求显式携带当前任务上下文，禁用自动重试；取消/超时和未知用量分别记录。
"""

import asyncio
import importlib.metadata
import json
import socket
import ssl
from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx2
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)
from pydantic import SecretStr

from repoagent.execution.contracts import ModelCallRecord, ModelRequest
from repoagent.execution.prompt_spec import CITATION_BOUNDARY as CITATION_BOUNDARY
from repoagent.execution.prompt_spec import PROMPT_VERSION
from repoagent.models.base import (
    CallOutcome,
    ModelBudgetError,
    ModelCancelledError,
    ModelServiceError,
    ModelTimeoutError,
    PreparedCall,
)
from repoagent.models.chat_payload import build_payload as build_payload
from repoagent.models.chat_payload import compact_schema as compact_schema
from repoagent.models.chat_payload import wire_feedback as wire_feedback
from repoagent.models.chat_response import decode_chat_completion
from repoagent.models.config import ModelConfig
from repoagent.models.response_json import decode_response_json
from repoagent.models.response_json import encode_json as encode
from repoagent.tools.schema import SCHEMA_VERSION


def safe_transport_failure(error: BaseException) -> str:
    """只按白名单类型分类异常链；不序列化异常文字，也不据此判定服务端根因。"""
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 12:
        chain.append(error)
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    categories = (
        (socket.gaierror, "dns"),
        (ssl.SSLError, "tls"),
        (httpx2.ProxyError, "proxy"),
        (httpx2.ConnectTimeout, "connect_timeout"),
        (httpx2.ReadTimeout, "read_timeout"),
        (httpx2.WriteTimeout, "write_timeout"),
        (httpx2.PoolTimeout, "pool_timeout"),
        (httpx2.RemoteProtocolError, "remote_protocol"),
        (httpx2.ConnectError, "connect"),
        (httpx2.ReadError, "read"),
        (httpx2.WriteError, "write"),
        (RuntimeError, "runtime"),
    )
    for kind, category in categories:
        if any(isinstance(item, kind) for item in chain):
            return category
    return "unknown"


class IsolatedChatClient(AsyncOpenAI):
    """限制默认HTTP头的兼容SDK客户端，避免转发其他供应商组织/认证配置。"""

    @property
    def default_headers(self) -> dict[str, str]:
        # 不把其他OpenAI项目的环境组织/自定义认证头带到已配置服务。
        """返回发往已配置服务的必要头；Authorization仅在传输层使用，不进入配置轨迹。"""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "Authorization": f"Bearer {self.api_key}",
        }


def response_json(content):
    """Only unwrap a whole JSON fence; never extract or complete partial objects."""
    return decode_response_json(content)[0]


class ChatCompletionsAdapter:
    native_first = True
    content_review = True
    """无任务状态的真实适配器：转换协议、单次SDK发送和本次用量解析。"""

    simulated = False
    format_repair = True

    def __init__(
        self,
        config: ModelConfig,
        *,
        api_key: str,
        client_factory=None,
        native_thinking="default",
        native_first=True,
        content_review=True,
    ):
        """复制并校验配置；密钥由组合入口提供，构造不联网。"""
        self.config = ModelConfig.model_validate(config.model_dump())
        self.config.capabilities.thinking_body(native_thinking)
        self.native_thinking = native_thinking
        self.native_first, self.content_review = native_first, content_review
        if not api_key.strip():
            raise ValueError("Selected model key is not configured")
        self._api_key = SecretStr(api_key)
        self.model_id = self.config.model
        self.request_timeout = self.config.request_timeout_seconds
        self._client_factory = client_factory
        self._transport = None
        from repoagent.models.cache_context import CachePolicy
        self.cache_policy = CachePolicy.named("combined")

    @property
    def configuration(self) -> dict:
        """返回解析后的非敏感生成配置，迁移描述不参与生成身份。"""
        return self.config.model_dump() | {
            "adapter_version": "chat-completions-v3-stream-pool",
            "native_thinking": self.native_thinking,
            "investigation_policy": {"native_first": self.native_first, "content_review": self.content_review},
            "cache_policy": vars(self.cache_policy),
            "prompt_version": PROMPT_VERSION,
            "tool_schema_version": SCHEMA_VERSION,
            "input_estimation": "sdk_json_utf8_bytes",
            "sdk": "openai",
            "sdk_version": importlib.metadata.version("openai"),
            "max_retries": 0,
            "account_source": "API account associated with the configured local key",
        }

    def with_native_thinking(self, selected):
        """任务开始时复制适配器；不修改已准备或在途请求的生成身份。"""
        return type(self)(
            self.config,
            api_key=self._api_key.get_secret_value(),
            client_factory=self._client_factory,
            native_thinking=selected,
            native_first=self.native_first,
            content_review=self.content_review,
        )

    def input_size(self, request: ModelRequest, max_tokens: int | None) -> int:
        # SDK实际序列化键名extra_body会展开；此处包括完整工具及输出schema，偏保守。
        """按request、max_tokens构造完整候选载荷并返回UTF-8字节数，不等于真实计费token。"""
        return len(
            encode(
                build_payload(
                    request,
                    self.config,
                    self.config.capabilities.output_for(max_tokens),
                    native_thinking=self.native_thinking,
                )
            ).encode("utf-8")
        )

    @property
    def service_identity(self) -> dict:
        """账本绑定的非敏感服务/账户范围；不使用密钥摘要冒充账户身份。"""
        return {
            "provider": self.config.provider,
            "endpoint": self.config.base_url,
            "account_scope": self.config.account_scope,
            "model": self.model_id,
        }

    def prepare(
        self, request: ModelRequest, max_output: int | None, *, continuation=()
    ) -> PreparedCall:
        """按既有提示/线格式准备不可变参数，无网络或费用副作用。"""
        resolved = self.config.capabilities.output_for(max_output)
        return PreparedCall.create(
            request,
            self.configuration,
            encode(
                build_payload(
                    request,
                    self.config,
                    resolved,
                    native_thinking=self.native_thinking,
                    continuation=continuation,
                    static_schema=self.cache_policy.schema,
                )
            ).encode("utf-8"),
            resolved,
            "explicit"
            if max_output is not None
            else "service_default"
            if resolved is None
            else "model_configuration",
        )

    def _client(self, timeout: float) -> AsyncOpenAI:
        """timeout是网络空闲上限；连接另限15秒，禁重试／重定向。"""
        if self._client_factory is not None:
            return self._client_factory(timeout)
        network_timeout = httpx2.Timeout(timeout, connect=min(15.0, timeout))
        return IsolatedChatClient(
            api_key=self._api_key.get_secret_value(),
            base_url=self.config.base_url,
            organization="",
            project="",
            max_retries=0,
            timeout=network_timeout,
            http_client=httpx2.AsyncClient(timeout=network_timeout, follow_redirects=False),
        )

    def begin_transport(self):
        from repoagent.models.transport import RequestTransport
        if self._transport is not None:
            raise ValueError("Transport already bound")
        self._transport = RequestTransport(self._client, self.request_timeout)

    def close(self):
        if self._transport is not None:
            self._transport.close()

    @asynccontextmanager
    async def _client_lease(self, timeout):
        if self._transport is not None:
            yield self._transport.client
        else:
            async with self._client(timeout) as client:
                yield client

    async def _send(self, payload: dict, timeout: float, cancelled: Callable[[], bool], on_delta=None):
        """timeout仅限制调用方总期限；SDK按网络读写空闲计时。

        退出时取消尚未完成的本地任务并等待清理；不保证远端立即停止生成或计费。
        """
        if cancelled():
            raise ModelCancelledError(code="cancelled")
        # 长响应可跨多个空闲窗口；包括SSE保活在内的原始网络数据由HTTP层计量。
        async with self._client_lease(self.request_timeout) as client:
            # 实际SDK调用在异步任务中进行，宿主可定期检查取消与剩余时间。
            async def receive():
                result = await client.chat.completions.create(**payload)
                if not payload.get("stream"):
                    return result
                from openai.types.chat import ChatCompletion

                from repoagent.models.streaming import DraftDecoder
                decoder = DraftDecoder()
                content, reasoning, calls = "", "", {}
                usage, finish, identity, returned_model = None, None, None, payload["model"]
                total_size = 0
                async for chunk in result:
                    identity, returned_model = chunk.id, chunk.model
                    if chunk.usage is not None:
                        usage = chunk.usage.model_dump()
                    if len(chunk.choices) > 1:
                        raise ModelServiceError(code="invalid_model_response")
                    for choice in chunk.choices:
                        finish = choice.finish_reason or finish
                        delta = choice.delta
                        text = delta.content or ""
                        content += text
                        reasoning += getattr(delta, "reasoning_content", "") or ""
                        total_size += len(text) + len(getattr(delta, "reasoning_content", "") or "")
                        for call in delta.tool_calls or []:
                            target = calls.setdefault(call.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if call.id:
                                target["id"] = call.id
                            if call.function:
                                target["function"]["name"] += call.function.name or ""
                                target["function"]["arguments"] += call.function.arguments or ""
                                total_size += len(call.function.arguments or "")
                        if total_size > 2_000_000 or len(calls) > 8:
                            raise ModelBudgetError(code="stream_capacity")
                        visible = decoder.feed(text)
                        if visible and on_delta:
                            on_delta(visible)
                if not finish:
                    raise ModelServiceError(code="stream_incomplete")
                return ChatCompletion.model_validate({
                    "id": identity, "object": "chat.completion", "created": 0,
                    "model": returned_model, "usage": usage,
                    "choices": [{"index": 0, "finish_reason": finish, "message": {
                        "role": "assistant", "content": content or None,
                        "reasoning_content": reasoning,
                        "tool_calls": [calls[i] for i in sorted(calls)] or None,
                    }}],
                })
            pending = asyncio.create_task(receive())
            deadline = asyncio.get_running_loop().time() + timeout
            try:
                while True:
                    if cancelled():
                        raise ModelCancelledError(code="cancelled")
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise ModelTimeoutError(code="request_timeout")
                    done, _ = await asyncio.wait(
                        {pending}, timeout=min(0.05, remaining)
                    )
                    if done:
                        return pending.result()
            # 离开等待循环前清理本地在途任务；请求状态和实际用量由ModelSession记录。
            finally:
                if not pending.done():
                    # 取消的是本地在途等待；供应商可能已处理请求，因此用量不能直接当作零。
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        pass

    def _send_prepared(self, prepared, *, timeout, cancelled, permit, on_delta=None) -> CallOutcome:
        """内部单次发送；消费会话许可，任何预期失败携带本次记录。"""
        permit.consume(prepared)
        request = ModelRequest.model_validate_json(prepared.request_json)
        payload = json.loads(prepared.payload)
        record = ModelCallRecord(
            record_version="model-call-v4-current",
            dispatch_state="not_sent",
            provider=self.config.provider,
            requested_model=self.model_id,
            wire_sha256=prepared.wire_sha256,
            requested_output_limit=prepared.max_output,
            output_limit_source=prepared.output_limit_source,
            native_thinking=self.native_thinking,
        )
        try:
            if timeout <= 0:
                raise ModelTimeoutError(code="request_timeout")
            if cancelled():
                raise ModelCancelledError(code="cancelled")
            # 进入SDK发送路径后，异常也按可能已发送处理；无用量时保留可能已发送的未知记录。
            record.dispatch_state = "possibly_sent"
            coroutine = self._send(payload, timeout, cancelled, on_delta)
            result = self._transport.run(coroutine) if self._transport is not None else asyncio.run(coroutine)
        except ModelServiceError as error:
            error.protocol_state = None
            error.record = record
            record.error_code = error.code
            record.billing_status = (
                "unknown" if record.input_tokens is None else "not_calculated"
            )
            raise
        except (APITimeoutError, httpx2.TimeoutException) as error:
            record.transport_failure = safe_transport_failure(error)
            record.error_code, record.billing_status = "request_timeout", "unknown"
            raise ModelTimeoutError(code="request_timeout", record=record) from None
        except AuthenticationError:
            record.error_code, record.billing_status = "authentication_error", "unknown"
            raise ModelServiceError(
                code="authentication_error", record=record
            ) from None
        except RateLimitError:
            record.error_code, record.billing_status = "rate_limited", "unknown"
            raise ModelServiceError(code="rate_limited", record=record) from None
        except APIConnectionError as error:
            record.transport_failure = safe_transport_failure(error)
            record.error_code, record.billing_status = (
                "model_connection_error",
                "unknown",
            )
            raise ModelServiceError(
                code="model_connection_error", record=record
            ) from None
        except APIStatusError as error:
            record.error_code, record.billing_status = (
                f"model_http_{error.status_code}",
                "unknown",
            )
            # 仅识别已知错误码，不将服务错误正文、源码或凭据写入诊断。
            if error.status_code == 400 and error.code in {
                "context_length_exceeded",
                "insufficient_context_window",
            }:
                record.error_code = "model_context_exceeded"
            raise ModelServiceError(code=record.error_code, record=record) from None

        return decode_chat_completion(result, request, record, self.config, self.native_thinking)
