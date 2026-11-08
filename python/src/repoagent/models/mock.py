"""脚本模拟模型：按顺序消费预先准备的响应，不调用网络也不理解问题。

用于验证执行控制、工具反馈和错误路径；模拟成功不能作为真实模型质量成绩。
"""

import json
from copy import deepcopy

from repoagent.execution.contracts import ModelCallRecord, ModelRequest, ModelResponse
from repoagent.models.base import CallOutcome, PreparedCall
from repoagent.models.base import ModelServiceError as ModelServiceError


class ScriptedModel:
    """确定性脚本替身：按顺序给响应，不根据问题或证据自动推理。"""

    model_id = "scripted-mock-v1"
    simulated = True
    format_repair = False
    request_timeout = 60.0

    @property
    def configuration(self):
        return {"provider": "scripted", "model": self.model_id, "simulated": True}

    def prepare(self, request, max_output):
        return PreparedCall.create(
            request,
            self.configuration,
            json.dumps(request.model_dump(), ensure_ascii=False).encode(),
            max_output,
        )

    def _send_prepared(self, prepared, *, timeout, cancelled, permit):
        permit.consume(prepared)
        request = ModelRequest.model_validate_json(prepared.request_json)
        return CallOutcome(
            self.complete(request),
            ModelCallRecord(
                record_version="model-call-v4-current",
                dispatch_state="not_sent",
                provider="scripted",
                requested_model=self.model_id,
                wire_sha256=prepared.wire_sha256,
            ),
        )

    def __init__(self, responses: list[dict | ModelResponse | ModelServiceError]):
        """深拷贝responses避免外部修改影响脚本，并创建独立请求记录列表。"""
        self.responses = deepcopy(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        """记录request副本并消费下一条响应，校验后返回ModelResponse。

        脚本项为服务异常则抛出，脚本耗尽也作为服务故障；无网络调用。
        """
        self.requests.append(request.model_copy(deep=True))
        if not self.responses:
            raise ModelServiceError("Mock script exhausted")
        item = self.responses.pop(0)
        if isinstance(item, ModelServiceError):
            raise item
        if isinstance(item, ModelResponse):
            item = item.model_dump()
        return ModelResponse.model_validate(item)
