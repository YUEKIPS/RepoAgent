"""Decode one received provider response and preserve its accounting and continuation."""

import json

from pydantic import ValidationError

from repoagent.execution.contracts import (
    CitationPatch,
    ContentReview,
    ModelCallRecord,
    ModelResponse,
    Plan,
    SummaryResult,
    ToolCall,
    WireAnswer,
    materialize_answer,
)
from repoagent.execution.delivery.repair import is_citation_patch
from repoagent.models.base import CallOutcome, ModelServiceError
from repoagent.models.response_json import (
    decode_response_json,
    encode_json,
    syntax_diagnostic,
)


def record_response_usage(result, record, usage_mapping):
    # 先记录服务身份与用量，再解析内容；模型输出坏JSON也不抹掉调用消耗。
    record.dispatch_state = "response_received"
    record.response_id = result.id
    record.request_id = getattr(result, "_request_id", None)
    record.returned_model = result.model
    if result.usage is not None:
        raw_usage = result.usage.model_dump()
        for name, path in usage_mapping.items():
            value = raw_usage
            for part in path.split(".") if path else []:
                value = value.get(part) if isinstance(value, dict) else None
            setattr(record, name, value if path else None)
    else:
        record.billing_status = "unknown"
    invalid_usage = []
    for name in usage_mapping:
        value = getattr(record, name)
        if value is not None and (type(value) is not int or value < 0):
            setattr(record, name, None)
            invalid_usage.append(name)
    if (
        record.input_tokens is not None
        and record.cache_hit_input_tokens is not None
        and record.cache_miss_input_tokens is not None
        and record.cache_hit_input_tokens + record.cache_miss_input_tokens
        != record.input_tokens
    ):
        record.cache_hit_input_tokens = record.cache_miss_input_tokens = None
        record.validation_errors.append("inconsistent_cache_usage")
    if (
        record.reasoning_tokens is not None
        and record.output_tokens is not None
        and record.reasoning_tokens > record.output_tokens
    ):
        record.reasoning_tokens = None
        record.validation_errors.append("inconsistent_reasoning_usage")
    if invalid_usage:
        record.validation_errors += [
            "invalid_usage:" + n for n in invalid_usage
        ]
        raise ModelServiceError(code="invalid_model_usage", record=record)
    try:
        return ModelCallRecord.model_validate(record.model_dump())
    except ValidationError:
        record.input_tokens = record.output_tokens = None
        record.cache_hit_input_tokens = record.cache_miss_input_tokens = None
        raise ModelServiceError(
            code="invalid_model_usage", record=record
        ) from None


def decode_chat_completion(result, request, record, config, native_thinking):
    protocol_state = None
    try:
        record = record_response_usage(result, record, config.usage_mapping.model_dump())
        if len(result.choices) != 1:
            raise ModelServiceError(code="invalid_model_response")
        choice = result.choices[0]
        record.finish_reason = choice.finish_reason
        if choice.finish_reason == "length":
            raise ModelServiceError(code="model_output_truncated")
        if choice.finish_reason not in {"stop", "tool_calls"}:
            raise ModelServiceError(code="model_response_rejected")
        message = choice.message
        if (
            request.stage == "act"
            and request.available_tools
            and config.capabilities.thinking_format == "deepseek"
            and native_thinking != "off"
        ):
            reasoning = getattr(message, "reasoning_content", None)
            if not isinstance(reasoning, str):
                raise ModelServiceError(code="missing_protocol_continuation")
            assistant = {
                "role": "assistant",
                "content": message.content,
                "reasoning_content": reasoning,
            }
            if message.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": item.id,
                        "type": item.type,
                        "function": {
                            "name": item.function.name,
                            "arguments": item.function.arguments,
                        },
                    }
                    for item in message.tool_calls
                ]
            protocol_state = encode_json(assistant).encode("utf-8")
        # 原生function调用与最终JSON回答分开解析；模型只能提议，宿主后续才校验并执行。
        if message.tool_calls:
            if request.stage != "act" or choice.finish_reason != "tool_calls":
                raise ModelServiceError(code="unexpected_model_response")
            calls = []
            for item in message.tool_calls:
                if item.type != "function":
                    raise ModelServiceError(code="invalid_model_response")
                try:
                    arguments = json.loads(item.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    # 无法构造受校验的工具观测，不能伪造tool消息后继续思考链。
                    raise ModelServiceError(
                        code="invalid_model_tool_arguments"
                    ) from None
                calls.append(
                    ToolCall(
                        call_id=item.id,
                        name=item.function.name,
                        arguments=arguments,
                    )
                )
            return CallOutcome(
                ModelResponse(kind="tool_calls", tool_calls=calls),
                record,
                protocol_state,
            )
        if choice.finish_reason != "stop" or not message.content:
            raise ModelServiceError(code="empty_model_response")
        raw, normalized = decode_response_json(
            message.content,
            recover_controls=request.stage in {"act", "answer"} and not is_citation_patch(request.repair_feedback),
        )
        if normalized:
            record.response_recovery = "json_string_control_characters"
        # 只接受当前直接对象；内部响应类型不属于模型线格式。
        if is_citation_patch(request.repair_feedback):
            response = ModelResponse(kind="citation_patch", citation_patch=CitationPatch.model_validate(raw))
            record.response_shape = "citation_patch_object"
        elif request.stage == "summary":
            response = ModelResponse(kind="summary", summary=SummaryResult.model_validate(raw))
            record.response_shape = "summary_object"
        elif request.stage == "plan":
            response = ModelResponse(kind="plan", plan=Plan.model_validate(raw))
            record.response_shape = "plan_object"
        elif request.stage == "review":
            response = ModelResponse(kind="review", review=ContentReview.model_validate(raw))
            record.response_shape = "review_object"
        else:
            response = ModelResponse(
                kind="answer", answer=materialize_answer(WireAnswer.model_validate(raw), request.evidence)
            )
            record.response_shape = "answer_object"
        expected = "citation_patch" if is_citation_patch(request.repair_feedback) else request.stage if request.stage in {"plan", "summary", "review"} else "answer"
        if response.kind != expected:
            raise ModelServiceError(code="unexpected_model_response")
        return CallOutcome(response, record, protocol_state)
    except ModelServiceError as error:
        error.protocol_state = protocol_state
        error.record = record
        record.error_code = error.code
        record.billing_status = (
            "unknown" if record.input_tokens is None else "not_calculated"
        )
        raise
    except ValidationError as error:
        record.validation_stage = "schema"
        # 仅记录错误类型与已知契约字段，不保留模型原文、输入值或错误上下文。
        fields = {
            "kind",
            "plan",
            "answer",
            "answer_status",
            "claims",
            "citations",
            "limitations",
            "claim_id",
            "claim_ids",
            "text",
            "evidence_id",
            "commit",
            "path",
            "start_line",
            "end_line",
            "tool_call",
            "tool_calls",
            "call_id",
            "name",
            "arguments",
            "queries",
        }
        record.validation_errors = [
            item["type"]
            + ":"
            + ".".join(
                str(part)
                if isinstance(part, int) or part in fields
                else "unknown_field"
                for part in item["loc"]
            )
            for item in error.errors(
                include_input=False, include_context=False, include_url=False
            )[:12]
        ]
        record.error_code = "invalid_model_response"
        record.billing_status = (
            "unknown" if record.input_tokens is None else "not_calculated"
        )
        failure = ModelServiceError(code="invalid_model_response", record=record)
        failure.protocol_state = protocol_state
        raise failure from None
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        record.validation_stage = "json_syntax"
        record.validation_errors = ([syntax_diagnostic(error)] if isinstance(error, json.JSONDecodeError)
                                    else ["json_syntax:invalid_unicode"])
        record.error_code = "invalid_model_response"
        record.billing_status = (
            "unknown" if record.input_tokens is None else "not_calculated"
        )
        failure = ModelServiceError(code="invalid_model_response", record=record)
        failure.protocol_state = protocol_state
        raise failure from None
