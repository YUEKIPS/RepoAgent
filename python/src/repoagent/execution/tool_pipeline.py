"""Transform tool results into paired observations and citable source text."""

from repoagent.execution.contracts import Observation, ToolCall, ToolOutcome
from repoagent.execution.control import StopExecution
from repoagent.repositories.text import TEXT_ERRORS


class ToolPipeline:
    def __init__(self, state, limits, tools, evidence, limitations, registry=None):
        self.state, self.limits, self.tools = state, limits, tools
        self.evidence, self.limitations = evidence, limitations
        self.registry = registry

    def observe(self, call: ToolCall, outcome: ToolOutcome):
        """把call/outcome转为下一轮可见观测，并累计读取证据。

        不可恢复工具故障立即停止；读取文本移入Evidence避免重复发送。
        列表观测裁剪保留续页位置，原始结果仍在trace中。
        """
        raw_outcome = outcome
        if not outcome.ok and not outcome.error.recoverable:
            raise StopExecution("FAILED", "tool_failure")
        if not outcome.ok and outcome.error.code in TEXT_ERRORS:
            self.limitations.append(
                f"文件读取受限：{call.arguments.get('repo_id', '')} / {call.arguments.get('path', '')}；"
                f"原因 {outcome.error.code}。该文件未取得可引用原文，请补查其他文件或说明证据不足。"
            )
        if outcome.ok and (not self.registry or self.registry.spec(call.name).origin == "builtin"):
            normalizer = self.registry.spec(call.name).normalizer if self.registry else None
            data = normalizer(outcome.result) if normalizer else outcome.result.copy()
            # 固定快照身份已在query/Evidence中；完整原始工具结果仍保留trace。
            for key in ("snapshot_id", "commit", "state", "coverage"):
                data.pop(key, None)
            outcome = outcome.model_copy(update={"result": data})
        self.state.record_observation(Observation(call=call, outcome=outcome), raw_outcome)
