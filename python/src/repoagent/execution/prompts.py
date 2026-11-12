"""Host-owned business rules; stage rules remain stable across tool decisions."""

from repoagent.execution.contracts import Query
from repoagent.execution.prompt_spec import (
    ACT_GUIDANCE,
    ANSWER_GUIDANCE,
    CONVERSATION_GUIDANCE,
    EVIDENCE_RULES,
    FINAL_GUIDANCE,
    INSTRUCTION,
    REVIEW_GUIDANCE,
)


def answer_guidance(query: Query) -> str:
    style = {
        "auto": "详略随问题复杂度变化。",
        "brief": "用户选择简洁：保留结论、关键条件和必要依据，省略背景。",
        "detailed": "用户选择详解：逐步解释实现、条件与作用，可举有依据的例子。",
    }[query.policy.answer_style]
    return ANSWER_GUIDANCE + style


def request_instruction(stage, query, state, usage, limits, finalization_reason, source_failure, repair_notice):
    can_use_tools = (stage == "act" and limits.max_model_requests - usage.model_requests > 1
                     and usage.tool_calls < limits.max_tool_calls and finalization_reason is None)
    instruction = INSTRUCTION
    if stage == "act":
        instruction += ACT_GUIDANCE if can_use_tools else FINAL_GUIDANCE
    elif stage == "review":
        instruction += REVIEW_GUIDANCE
    elif stage == "answer":
        instruction += "基线本轮不能请求追加工具，使用提供证据作答。"
    if query.conversation:
        instruction += CONVERSATION_GUIDANCE
    guidance = answer_guidance(query) if stage not in {"plan", "review"} else ""
    return instruction + guidance + repair_notice, guidance, can_use_tools


def source_answer_guidance(request):
    if request.stage == "plan":
        return "目录提示是导航，不是源码；无内容缓存时优先路径检索，不在规划阶段作答。"
    return (
        EVIDENCE_RULES
        + "github_lazy的路径与目录覆盖固定catalog，text/symbol仅覆盖已缓存索引。cursor_stale时从cursor=0重搜。"
        "read_file须用允许的repo_id；文件读取错误表示未读，不表示不存在。"
        "跨仓关联需读取双方相关入口及必要prefix/baseURL/代理配置，缺部署或运行证据时不确定断言运行链路。"
        "工具声明索引不等于精确调用图或框架运行语义；TSX/JSX可有声明索引，Vue等以宿主实际能力为准。"
        "没有原文时使用insufficient_evidence及空claims/citations，把搜索范围和缺口写入正文。"
    )
