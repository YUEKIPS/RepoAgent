"""模型适配协议与分类异常：会话以不可变请求和单次结果隔离厂商差异。

异常code用于结果分类，调用方不应把供应商原始错误或密钥写入公开轨迹。
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from repoagent.execution.contracts import ModelCallRecord, ModelRequest, ModelResponse


class ModelServiceError(RuntimeError):
    """可预期模型服务错误；code供执行器分类，不要求向用户展示原始message。"""

    def __init__(
        self,
        message: str = "Model service failed",
        *,
        code="model_service_error",
        record: ModelCallRecord | None = None,
    ):
        """保存安全错误码code及异常message；适配层应先脱敏再构造。"""
        super().__init__(message)
        self.code = code
        self.record = record


class ModelCancelledError(ModelServiceError):
    """调用因用户取消终止；与服务故障分开映射到CANCELLED。"""

    pass


class ModelTimeoutError(ModelServiceError):
    """远程请求超时；执行器再判断是单请求超时还是任务deadline耗尽。"""

    pass


class ModelBudgetError(ModelServiceError):
    """token/容量/次数等约束拒绝继续，保留此前消耗而非清账。"""

    pass


def configuration_id(configuration: dict) -> str:
    """对非敏感生成配置做稳定摘要；不包含密钥、账本或任务状态。"""
    return hashlib.sha256(
        json.dumps(configuration, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class PreparedCall:
    """一次纯本地准备产物；bytes避免frozen对象内的dict仍可被修改。"""

    call_id: str
    configuration_id: str
    payload: bytes
    request_json: bytes
    max_output: int | None
    input_size: int
    output_limit_source: str = "explicit"
    measurement_json: bytes = b"{}"

    @property
    def measurement(self):
        return json.loads(self.measurement_json)

    @property
    def wire_sha256(self) -> str:
        """规范化SDK参数摘要，不声称为最终HTTP字节摘要。"""
        return hashlib.sha256(self.payload).hexdigest()

    @classmethod
    def create(
        cls, request, configuration, payload, max_output, output_limit_source=None
    ):
        """构造独立准备产物；不请求服务、不预留费用。"""
        return cls(
            str(uuid4()),
            configuration_id(configuration),
            payload,
            request.model_dump_json().encode(),
            max_output,
            len(payload),
            output_limit_source
            or ("service_default" if max_output is None else "explicit"),
        )


class _DispatchPermit:
    """内部一次性发送许可；与会话已领取的一次发送票据对应，不是用户配置。"""

    def __init__(self, prepared):
        self._prepared = prepared
        self._used = False

    def consume(self, prepared):
        """适配器进入发送路径前校验；同一许可不能用于第二次发送。"""
        if self._used or prepared != self._prepared:
            raise ModelBudgetError(code="invalid_dispatch_permit")
        self._used = True


@dataclass(frozen=True)
class CallOutcome:
    """本次响应和本次记录；失败通过ModelServiceError.record携带记录。"""

    response: ModelResponse
    record: ModelCallRecord
    # 适配层的内部续接，不进入ModelResponse或公开轨迹。
    protocol_state: bytes | None = None


class ProviderAdapter(Protocol):
    """受控会话使用的协议；实现负责载荷转换和一次发送，不保存任务状态。"""

    model_id: str
    simulated: bool
    format_repair: bool
    request_timeout: float

    @property
    def configuration(self) -> dict: ...
    @property
    def service_identity(self) -> dict | None: ...
    def prepare(
        self, request: ModelRequest, max_output: int | None, *, continuation=()
    ) -> PreparedCall: ...
    def _send_prepared(
        self,
        prepared: PreparedCall,
        *,
        timeout: float,
        cancelled: Callable[[], bool],
        permit: _DispatchPermit,
        on_delta: Callable[[str], None] | None = None,
    ) -> CallOutcome: ...
