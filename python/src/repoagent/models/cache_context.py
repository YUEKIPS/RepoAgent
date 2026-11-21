"""Pure candidate rendering; only ModelSession commits a dispatched context.

Experimental switches are constructor policy, not user-facing provider parameters.
Source events are immutable data. The current control frame owns citable IDs.
"""

import json
from copy import deepcopy
from dataclasses import dataclass

from repoagent.execution.prompt_spec import PROTOCOL_CORRECTION, STABLE_SYSTEM
from repoagent.models.base import ModelBudgetError
from repoagent.models.response_json import encode_json as encoded


@dataclass(frozen=True)
class CachePolicy:
    tail: bool = False
    system: bool = False
    history: bool = False
    evidence: bool = False
    schema: bool = False

    @property
    def enabled(self):
        return any(vars(self).values())

    @classmethod
    def named(cls, name):
        if name in {"legacy", "baseline"}:
            return cls()
        if name == "combined":
            return cls(True, True, True, True, True)
        names = name.split("+")
        if any(n not in cls.__dataclass_fields__ for n in names):
            raise ValueError("Unknown cache experiment policy")
        return cls(**{n: True for n in names})


def native_messages(continuations, observations, *, allow_unexecuted=False):
    known = {o.call.call_id: o for o in observations if not (o.call.call_id or "").startswith("initial-")}
    messages, seen = [], set()
    for raw in continuations:
        assistant = json.loads(raw)
        messages.append(assistant)
        calls = assistant.get("tool_calls") or []
        for call in calls:
            identifier = call["id"]
            if identifier in seen or (identifier not in known and not allow_unexecuted):
                raise ModelBudgetError(code="continuation_context_incomplete")
            seen.add(identifier)
            if identifier not in known:
                messages.append({"role":"tool", "tool_call_id":identifier,
                                 "content":encoded({"call_id":identifier,"name":call["function"]["name"],
                                     "ok":False,"error":{"code":"host_tool_not_executed","recoverable":False},
                                     "host_note":"Retrieval is stopped. This call was NOT executed; no source result exists."})})
                continue
            messages.append({"role": "tool", "tool_call_id": identifier,
                             "content": encoded(known[identifier].outcome.model_dump(exclude_none=True))})
        if not calls:
            messages.append({"role": "user", "content": PROTOCOL_CORRECTION})
    if not set(known) <= seen:
        raise ModelBudgetError(code="continuation_context_incomplete")
    return messages


def render(payload, request, continuations, previous, policy):
    """No mutation of previous, request, prepared call or session. Returns a candidate."""
    payload, previous = deepcopy(payload), deepcopy(previous or {})
    if previous and previous.get("namespace") != request.evidence_namespace:
        previous = {}
    if request.stage == "review":
        from repoagent.models.review_cache import render_review
        return render_review(payload, request, previous, policy)
    if (request.repair_feedback or {}).get("mode") == "citation_patch":
        return payload, {"resume_context": previous}
    if request.stage == "summary":
        return payload, {}  # A different protocol intentionally begins a new prefix.
    system = payload["messages"][0]["content"]
    control = json.loads(payload["messages"][1]["content"])
    if policy.evidence or policy.system or policy.tail:
        control["evidence_namespace"] = request.evidence_namespace
        control["evidence_view_revision"] = request.evidence_view_revision
    rules = previous.get("protocol_rules", []) if policy.system else []
    new_rules = []
    if policy.system:
        payload["messages"][0]["content"] = STABLE_SYSTEM
        existing = next((r for r in rules if r["protocol_rules"] == system), None)
        if existing is None:
            existing = {"rule_id": f"R{len(rules)+1}", "protocol_rules": system}
            rules.append(existing)
            new_rules.append({"role":"user", "content":encoded({"host_protocol_rule":existing})})
        control["protocol_rules_ref"] = existing["rule_id"]
        control["stage"] = request.stage
    context = {"host_control": control} if policy.system or policy.tail else control
    query = request.query.model_dump(exclude={"policy"})
    raw_continuations = [s.decode() if isinstance(s, bytes) else s for s in continuations]
    tools_enabled = bool(request.available_tools)
    if policy.system or policy.tail:
        control["tools_enabled"] = tools_enabled
    if policy.history and request.stage == "act" and not request.force_no_tools:
        tools = payload.get("tools") or previous.get("wire_tools", [])
        if tools:
            payload["tools"] = tools
            # DeepSeek changes its rendered prefix with tool_choice=none
            # (inferred from matched token/prefix probes). Keep the wire
            # template stable; latest host control and Execution still forbid
            # actual tool execution when this round has no available tools.
            payload["tool_choice"] = "auto"
        control.pop("completed_observations", None)
    can_preserve = request.stage == "act" and (tools_enabled or policy.history) and not request.force_no_tools
    # A continuation prefix is rebased only by explicit compaction. Rebuild then.
    old = previous.get("continuations", [])
    reusable = (previous.get("stage") == "act" and previous.get("query") == query
                and raw_continuations[:len(old)] == old and can_preserve
                and previous.get("namespace") == request.evidence_namespace
                and not request.evidence_rebase)
    full_native = native_messages(continuations, request.observations,
                                  allow_unexecuted=not tools_enabled) if can_preserve else []
    old_native_count = previous.get("native_message_count", 0) if reusable else 0
    known_sources = previous.get("known_sources", {}) if policy.evidence else {}
    sources = {}
    events = []
    new_events = []
    if policy.evidence:
        for item in control.pop("evidence", []):
            identifier = item["evidence_id"]
            if identifier in known_sources and known_sources[identifier] != item:
                raise ModelBudgetError(code="immutable_evidence_changed")
            known_sources[identifier] = item
            sources[identifier] = item
            event = {"role": "user", "content": encoded({"evidence_namespace": request.evidence_namespace, "source_event": item})}
            events.append(event)
            if identifier not in previous.get("sources", {}):
                new_events.append(event)
        if set(previous.get("sources", {})) - set(sources):
            reusable = False
        # Rebuilt native batches use projected results. Built-in read_file text
        # is already normalized into source events by the host ToolPipeline.
    positions = dict(previous.get("evidence_positions", {})) if reusable else {}
    if policy.tail:
        control.pop("query", None)
        if reusable and previous.get("tail"):
            messages = previous["messages"]
            messages[0] = payload["messages"][0]
            messages += full_native[old_native_count:]
        else:
            messages = [payload["messages"][0], {"role": "user", "content": encoded({"query": query})}]
            messages += full_native
            new_events = events
            new_rules = [{"role":"user", "content":encoded({"host_protocol_rule":r})} for r in rules]
        messages += new_rules
        for event in new_events:
            identifier = json.loads(event["content"])["source_event"]["evidence_id"]
            positions[identifier] = len(messages)
            messages.append(event)
        messages.append({"role": "user", "content": encoded(context)})
    else:
        messages = [payload["messages"][0], {"role": "user", "content": encoded(context)}]
        messages += [{"role":"user", "content":encoded({"host_protocol_rule":r})} for r in rules]
        for event in events:
            positions[json.loads(event["content"])["source_event"]["evidence_id"]] = len(messages)
            messages.append(event)
        messages += full_native if can_preserve else payload["messages"][2:]
    payload["messages"] = messages
    from repoagent.models.review_cache import cache_profile

    state = {"messages": messages, "stage": request.stage, "query": query,
             "continuations": raw_continuations, "native_message_count": len(full_native),
             "sources": sources, "source_events": events, "tail": policy.tail,
             "known_sources": known_sources, "evidence_positions": positions,
             "namespace": request.evidence_namespace,
             "protocol_rules": rules,
             "wire_tools": payload.get("tools", previous.get("wire_tools", [])),
             "wire_profile": cache_profile(payload),
             "wire_template": {k: deepcopy(payload[k]) for k in ("tools", "tool_choice", "response_format") if k in payload}}
    return payload, state
