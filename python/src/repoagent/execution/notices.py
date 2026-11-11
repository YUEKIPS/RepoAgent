"""按已知程序来源分类；模型限制原文保留，不猜测其语义是否影响结论。"""

from repoagent.execution.contracts import Notice

PROGRAM_NOTICES = {
    "模拟模型结果，仅验证工程流程；未验证回答质量。": (
        "evaluation",
        "info",
        "simulated",
    ),
    "快照覆盖不完整；未搜到不代表功能不存在。": (
        "evidence",
        "warning",
        "partial_snapshot",
    ),
    "初始检索仅取top-k；更多命中未读取。": ("execution", "info", "initial_top_k"),
    "初始读取达到上限，剩余候选未读取；Agent可在额度内补查。": (
        "execution",
        "info",
        "initial_reads",
    ),
    "输入预算裁剪了旧工具观测；累计完整证据仍保留。": (
        "execution",
        "info",
        "old_observations_trimmed",
    ),
    "读取有截断；只提供完整行，可按continuation续读。": (
        "evidence",
        "warning",
        "read_truncated",
    ),
    "初始/累计证据受预算限制，部分内容未提供。": (
        "evidence",
        "warning",
        "evidence_budget",
    ),
    "工具观测受上下文限制裁剪；原始结果保留轨迹。": (
        "evidence",
        "warning",
        "tool_result_trimmed",
    ),
}
LIVE_NOTE = (
    "模型回答未经过独立人工语义评分；token用量按服务返回记录，缺失部分标为未知。"
)


def stop_notice(reason, *, finalizing=False):
    """仅返回程序可确认的原因，不包含供应商原始错误正文。"""
    labels = {
        "model_requests": "已达到本次模型调用次数边界。可缩小问题范围后手动再次提问。",
        "tool_calls": "已达到本次工具调用次数边界。可缩小问题范围或补充入口线索。",
        "no_progress": "检索未取得新的有效信息，本轮已停止新增取证。可补充文件或入口线索。",
        "deadline": "本次分析总时间已用尽。可缩小问题范围；重新提交前请核查已有用量。",
        "request_timeout": "请求超时，未能完成本次回答。请求可能已发送，请先核查用量，再决定是否重新提交。",
        "cancelled": "已按你的要求停止，不会自动继续。",
        "input_estimate": "当前问题、证据和观测超过输入容量。可缩小问题范围。",
        "context_protected_content": "已尝试精简上下文，但受保护的近期内容仍超出容量；原文保留，本轮未能完成。可调大输入预算或缩小问题范围。",
        "context_no_summary_budget": "上下文需要摘要，但本轮剩余额度不足或摘要未开启；原文保留。可调整预算后再次提问。",
        "context_summary_input": "待摘要内容本身超出模型输入容量，原文保留；可缩小范围后再次提问。",
        "context_no_progress": "本次摘要未降低上下文占用，已停止重复压缩；原文保留。",
        "run_record_failed": "执行步骤未能保存，本轮已停止继续请求模型。请检查本地存储。",
        "checkpoint_write_failed": "执行检查点未能保存，已停止后续请求；请检查本地存储。",
        "citation_repair_no_progress": "引用修正出现重复问题且没有新的有效依据，未发布这份候选。可查看未发布草稿。",
        "citation_patch_invalid": "局部引用修正未通过候选、范围或来源校验，未发布这份候选。",
        "citation_repair_budget": "引用尚未修正完成，本次调用额度已用尽，候选没有作为正式答案发布。",
        "model_usage_unknown": "本次模型请求的用量无法确认，已保留未知记录并停止继续发送；不会自动重发。",
        "model_session_halted": "模型请求状态尚未核清，本轮停止继续发送，避免重复生成。",
        "stream_incomplete": "模型输出流没有完整结束，候选未发布；请求用量可能未知，不会自动重发。",
        "output_tokens": "模型输出达到长度限制，未能完成回答。可缩小问题范围。",
        "output_estimate": "模型输出达到本次长度限制，未能完成回答。可缩小问题范围。",
        "tool_requested_after_finalization": "模型在工具已关闭后仍请求取证，本轮已停止，未执行该操作。",
        "invalid_call_id": "模型工具调用标识不合法，本轮已停止，未继续执行该批操作。",
        "invalid_model_response": "模型返回的内容不符合回答格式，未能发布答案。请查看任务详情。",
    }
    citation_errors = {
        "duplicate_claim_id", "claims_without_source_evidence", "unknown_evidence_id",
        "citation_source_mismatch", "citation_range_outside_evidence", "unknown_claim_id",
        "answered_claims_without_citations",
    }
    if finalizing:
        text = "已停止新增取证，仅依据本轮已取得证据收尾；未读内容和缺口请见回答。"
        if reason == "no_progress":
            text = "检索出现重复操作，已停止新增取证并依据已有证据收尾。"
    elif reason in citation_errors:
        text = "已生成草稿，但引用未通过来源或位置校验，因此未发布正式答案。请核对草稿和任务详情。"
    elif reason.startswith("model_http_"):
        text = "模型服务未能完成本次请求。请查看脱敏原因码并核查用量，再决定是否重新提交。"
    else:
        text = labels.get(reason, "本次分析未能完成，未发布正式答案。请查看任务详情；重新提交前请核查已有用量。")
    return Notice(source="program", category="execution", severity="warning",
                  code="retrieval_finalized" if finalizing else "execution_stopped", text=text)


def build_notices(program, model=(), file_issues=()):
    notices = []
    for text in dict.fromkeys(program):
        if file_issues and text.startswith("文件读取受限："):
            continue
        if text == LIVE_NOTE:
            notices.extend(
                [
                    Notice(
                        source="program",
                        category="evaluation",
                        severity="info",
                        code="not_scored",
                        text="模型回答未经过独立人工语义评分。",
                    ),
                    Notice(
                        source="program",
                        category="execution",
                        severity="info",
                        code="provider_reported_usage",
                        text="token用量按服务返回记录，缺失部分标为未知。",
                    ),
                ]
            )
            continue
        category, severity, code = PROGRAM_NOTICES.get(
            text, ("evidence", "warning", "unclassified_program_notice")
        )
        notices.append(
            Notice(
                source="program",
                category=category,
                severity=severity,
                code=code,
                text=text,
            )
        )
    notices.extend(
        Notice(
            source="model",
            category="answer",
            severity="warning",
            code="model_scope",
            text=text,
        )
        for text in dict.fromkeys(model)
    )
    seen = set()
    labels = {
        "unsupported_text_encoding": "文本编码不支持",
        "text_encoding_required": "需要指定文本编码",
        "text_encoding_conflict": "指定编码与文件标记冲突",
        "unsupported_text_characters": "文件包含不支持的控制字符",
        "decoded_text_limit": "解码文本超过单文件容量限制",
    }
    for observation in file_issues:
        error = observation.outcome.error
        path = observation.call.arguments.get("path")
        if (
            observation.call.name != "read_file"
            or not error
            or error.code not in labels
            or not isinstance(path, str)
        ):
            continue
        key = (observation.call.arguments.get("repo_id"), path, error.code)
        if key in seen:
            continue
        seen.add(key)
        notices.append(
            Notice(
                source="program",
                category="evidence",
                severity="warning",
                code=error.code,
                text=f"文件读取受限：{key[0] or ''} / {path}；{labels[error.code]}，相关实现尚未核查。",
            )
        )
    return notices
