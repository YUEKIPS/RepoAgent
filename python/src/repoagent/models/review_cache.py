"""Append a review tail to a safe, already-dispatched investigation prefix."""

import hashlib
import json
from copy import deepcopy

from repoagent.execution.prompt_spec import STABLE_SYSTEM
from repoagent.models.base import ModelBudgetError
from repoagent.models.evidence_view import numbered
from repoagent.models.response_json import encode_json as encoded

TEMPLATE_FIELDS = ("tools", "tool_choice", "response_format")
PROFILE_FIELDS = ("model", "thinking", "reasoning_effort", "reasoning")


def cache_profile(payload):
    """Native effort may render before system; messages alone are not a cache key."""
    wire = payload | payload.get("extra_body", {})
    return {field: deepcopy(wire[field]) for field in PROFILE_FIELDS if field in wire}


def message_data(message):
    if message.get("role") != "user":
        return {}
    try:
        value = json.loads(message.get("content", ""))
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def safe_prefix(previous, current, namespace):
    """Stop at stale candidates/cold source, never retain half a tool exchange."""
    messages, pending, boundary = [], set(), 0
    reason = "complete_investigation_prefix"
    for message in previous["messages"]:
        data = message_data(message)
        control = data.get("host_control", data)
        if "citation_repair" in control or "content_review" in control:
            reason = "old_candidate_frame"
            break
        if message.get("role") == "assistant" and not message.get("tool_calls"):
            reason = "old_assistant_candidate"
            break
        if "source_event" in data:
            if data.get("evidence_namespace") != namespace:
                raise ModelBudgetError(code="evidence_namespace_mismatch")
            event = data["source_event"]
            identifier = event["evidence_id"]
            if identifier not in current:
                reason = "source_removed_from_current_view"
                break
            if event != current[identifier]:
                raise ModelBudgetError(code="immutable_evidence_changed")
        if message.get("role") == "assistant":
            if pending:
                reason = "incomplete_tool_exchange"
                break
            pending = {call["id"] for call in message.get("tool_calls", [])}
        elif message.get("role") == "tool":
            identifier = message.get("tool_call_id")
            if identifier not in pending:
                reason = "incomplete_tool_exchange"
                break
            pending.remove(identifier)
        elif pending:
            reason = "incomplete_tool_exchange"
            break
        messages.append(deepcopy(message))
        if not pending:
            boundary = len(messages)
    if pending:
        reason = "incomplete_tool_exchange"
    return messages[:boundary], reason


def render_review(payload, request, previous, policy):
    """Review projection owns source positions; its commit preserves the act branch."""
    query = request.query.model_dump(exclude={"policy"})
    bound = (
        previous.get("stage") == "act"
        and previous.get("query") == query
        and previous.get("namespace") == request.evidence_namespace
    )
    preserved = deepcopy(previous) if bound else {}
    if not (policy.tail and policy.system and policy.history and policy.evidence):
        return payload, {
            "resume_context": preserved,
            "projection_metadata": {"mode": "review_inline_policy"},
        }
    current = {item.evidence_id: numbered(item) for item in request.evidence}
    for identifier, item in current.items():
        known = preserved.get("known_sources", {}).get(identifier)
        if known is not None and known != item:
            raise ModelBudgetError(code="immutable_evidence_changed")
    if bound and previous["wire_profile"] != cache_profile(payload):
        # DeepSeek renders native effort at the start of the conversation.
        # Retaining investigation history cannot recover that different prefix.
        return payload, {
            "resume_context": preserved,
            "projection_metadata": {
                "mode": "review_inline_profile_change",
                "prefix_boundary_reason": "native_profile_changed",
                "shared_message_count": 0,
                "shared_message_bytes": 0,
                "reused_source_count": 0,
                "appended_source_count": len(current),
            },
        }
    if bound and not request.evidence_rebase:
        messages, reason = safe_prefix(previous, current, request.evidence_namespace)
    else:
        messages, reason = (
            [],
            "pressure_rebase"
            if request.evidence_rebase
            else "no_bound_investigation_prefix",
        )
    reused = len(messages)
    prefix_bytes = len(encoded(messages).encode()) if messages else 0
    if not messages:
        messages = [
            {"role": "system", "content": STABLE_SYSTEM},
            {"role": "user", "content": encoded({"query": query})},
        ]
    positions = {}
    for index, message in enumerate(messages):
        data = message_data(message)
        if "source_event" in data:
            positions[data["source_event"]["evidence_id"]] = index
    reused_sources = len(positions)
    for identifier, item in current.items():
        if identifier not in positions:
            positions[identifier] = len(messages)
            messages.append(
                {
                    "role": "user",
                    "content": encoded(
                        {
                            "evidence_namespace": request.evidence_namespace,
                            "source_event": item,
                        }
                    ),
                }
            )
    # The old rules remain historical data; only the final frame selects this protocol.
    rule = payload["messages"][0]["content"]
    rule_id = "review-" + hashlib.sha256(rule.encode()).hexdigest()[:16]
    messages.append(
        {
            "role": "user",
            "content": encoded(
                {"host_protocol_rule": {"rule_id": rule_id, "protocol_rules": rule}}
            ),
        }
    )
    control = json.loads(payload["messages"][1]["content"])
    control.pop("evidence", None)
    control.pop("citation_repair", None)
    control.update(
        stage="review",
        tools_enabled=False,
        protocol_rules_ref=rule_id,
        evidence_namespace=request.evidence_namespace,
        evidence_view_revision=request.evidence_view_revision,
        review_context_policy={
            "current_candidate_only": True,
            "historical_frames_superseded": True,
        },
    )
    messages.append({"role": "user", "content": encoded({"host_control": control})})
    payload["messages"] = messages
    if bound:
        template = previous["wire_template"]
        for field in TEMPLATE_FIELDS:
            payload.pop(field, None)
            if field in template:
                payload[field] = deepcopy(template[field])
    return payload, {
        "resume_context": preserved,
        "evidence_positions": positions,
        "projection_metadata": {
            "mode": "review_safe_prefix",
            "shared_message_count": reused,
            "shared_message_bytes": prefix_bytes,
            "reused_source_count": reused_sources,
            "appended_source_count": len(current) - reused_sources,
            "prefix_boundary_reason": reason,
        },
    }
