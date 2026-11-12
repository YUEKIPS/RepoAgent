"""Task budgets: token pressure plus an independent serialized-byte guard."""

from pydantic import Field, model_validator

from repoagent.execution.contracts import StrictModel


class Limits(StrictModel):
    """任务默认值与服务器保护上限分开，前端可在范围内上下调整。"""

    max_model_requests: int = Field(default=20, ge=1, le=100)
    max_tool_calls: int = Field(default=20, ge=1, le=100)
    deadline_seconds: float | None = Field(default=None, gt=0, le=1800)
    max_input_estimate: int = Field(default=4000000, ge=1, le=16000000)
    context_window_tokens: int | None = Field(default=None, ge=1024)
    pressure_ratio: float = Field(default=0.8, gt=0, le=0.95)
    headroom_tokens: int | None = Field(default=None, ge=0)
    prune_threshold_chars: int = Field(default=8192, ge=128)
    prune_head_chars: int = Field(default=4096, ge=0)
    prune_tail_chars: int = Field(default=1024, ge=0)
    # 真实为完整响应token，mock为序列化字节；不能混作正文汉字数。
    max_output_estimate: int | None = Field(default=None, ge=1)
    plan_output_estimate: int | None = Field(default=None, ge=1)
    function_output_limits: dict[str, int] = Field(default_factory=dict)
    observation_estimate: int = Field(default=1000, ge=128, le=16000)
    top_k: int = Field(default=5, ge=1, le=5)
    initial_max_reads: int = Field(default=2, ge=1, le=5)
    summary_enabled: bool = True
    summary_output_tokens: int = Field(default=1800, ge=256, le=16000)
    recent_turns: int = Field(default=2, ge=1, le=10)
    cold_catalog_bytes: int = Field(default=6000, ge=256, le=40000)
    final_request_reserve: int = Field(default=2, ge=1, le=5)
    stream: bool = True
    content_review_enabled: bool = True
    content_review_output_tokens: int = Field(default=4096, ge=512, le=8192)
    content_review_followup_models: int = Field(default=2, ge=0, le=4)
    content_review_followup_tools: int = Field(default=4, ge=0, le=8)

    @model_validator(mode="after")
    def valid_function_limits(self):
        if self.prune_head_chars + self.prune_tail_chars + 43 > self.prune_threshold_chars:
            raise ValueError("Head/tail preview must fit its threshold")
        if any(
            k not in {"plan", "act", "answer", "summary", "review"} or type(v) is not int or v < 1
            for k, v in self.function_output_limits.items()
        ):
            raise ValueError(
                "Function output limits require a known stage and positive integer"
            )
        return self

    def output_for(self, stage: str) -> int | None:
        """阶段覆盖→通用请求→功能配置→自动；act也可能直接给出最终回答。"""
        if stage == "summary":
            return self.summary_output_tokens
        if stage == "review":
            return min(self.content_review_output_tokens, self.max_output_estimate) if self.max_output_estimate is not None else self.content_review_output_tokens
        if stage == "plan" and self.plan_output_estimate is not None:
            return self.plan_output_estimate
        if self.max_output_estimate is not None:
            return self.max_output_estimate
        return self.function_output_limits.get(stage)
