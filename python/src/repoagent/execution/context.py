"""Select model-visible history and observations, then prepare the exact request.

This policy has no authority to send requests or alter the cumulative source state.
"""

from dataclasses import dataclass

from repoagent.execution.contracts import Answer, ModelRequest
from repoagent.execution.prompts import request_instruction
from repoagent.execution.repair import is_citation_patch, patch_constraints
from repoagent.tools.protocol import tool_schemas


@dataclass(frozen=True)
class ContextBuild:
    message: ModelRequest
    prepared_call: object
    can_use_tools: bool
    preparation_count: int
    output_limit: int | None
    selection_revision: int = 0


class ContextBuilder:
    def build(
        self,
        *,
        stage,
        query,
        state,
        usage,
        limits,
        finalization_reason,
        source_failure,
        repair_notice,
        navigation_hints,
        session,
        stop_retrieval,
        trace,
        limitations,
        evidence_manager=None,
        repair_feedback=None,
        selection_query=None,
        registry=None,
        conversation_session=None,
        pressure=False,
        force_no_tools=False,
        minimal=False,
        source_restrictions=None,
        review_input=None,
        source_capabilities=None,
    ):
        instruction, guidance, can_use_tools = request_instruction(
            stage,
            query,
            state,
            usage,
            limits,
            finalization_reason,
            source_failure,
            repair_notice,
        )
        if stage == "review":
            # Completed repairs are a different protocol and may contain an older candidate.
            repair_feedback = None
        selected = evidence_manager.select(selection_query or query.question) if evidence_manager else state.evidence
        patch_mode = is_citation_patch(repair_feedback)
        if patch_mode:
            # 片段原文仍须进入本次请求，补丁只能使用实际发送的证据。
            referenced = {c["evidence_id"] for c in repair_feedback["candidate"]["citations"]}
            anchors = [e for e in state.evidence if e.evidence_id in referenced]
            selected = [e for e in state.evidence if e.evidence_id in referenced or any(
                (e.repo_id, e.snapshot_id, e.source_version_id, e.commit, e.path) ==
                (a.repo_id, a.snapshot_id, a.source_version_id, a.commit, a.path) for a in anchors)]
            can_use_tools = False
            repair_feedback = dict(repair_feedback)
            repair_feedback["constraints"] = patch_constraints(Answer.model_validate(repair_feedback["candidate"]), repair_feedback["issues"], selected)
        if force_no_tools or minimal:
            can_use_tools = False
        message = ModelRequest(
            source_capabilities=source_capabilities or {},
            investigation=state.investigation.model_dump(),
            review_input=review_input,
            evidence_namespace=session.task_id,
            evidence_view_revision=evidence_manager.revision + int([e.evidence_id for e in selected] != evidence_manager.active_ids) if evidence_manager else 0,
            evidence_rebase=pressure,
            force_no_tools=force_no_tools or minimal,
            retrieval_stop_reason=finalization_reason,
            source_restrictions=source_restrictions or {},
            navigation_hints=[] if minimal else navigation_hints,
            stage=stage,
            instruction=instruction,
            query=query.model_copy(update={"conversation": None}) if minimal else query,
            evidence=selected,
            cold_evidence=evidence_manager.catalog(active_ids=[e.evidence_id for e in selected]) if evidence_manager else [],
            repair_feedback=repair_feedback,
            stream=limits.stream and stage in {"act", "answer"} and session.on_delta is not None and not patch_mode,
            available_tools=(registry.schemas() if registry else tool_schemas(workspace=True)) if can_use_tools else [],
            observations=[
                o
                for o in state.observations
                if finalization_reason
                or not (o.call.call_id.startswith("initial-") and o.outcome.ok)
            ]
            if stage == "act" and not patch_mode
            else [],
            remaining_model_requests=limits.max_model_requests - usage.model_requests,
            remaining_tool_calls=limits.max_tool_calls - usage.tool_calls,
        ).model_copy(deep=True)
        message.cold_evidence_next_cursor = evidence_manager.catalog_next_cursor if evidence_manager else None
        if conversation_session and not patch_mode and not minimal:
            message.runtime_memory = conversation_session.runtime_memory(state.run_id)
            message.session_messages = conversation_session.prior_tool_messages(state.run_id)
        if pressure and registry:
            message.observations = [registry.project(o, limits) for o in message.observations]
        output_limit = limits.output_for(stage)
        prepared_call = session.prepare(message, output_limit)
        preparation_count = 1

        def rebuild():
            nonlocal prepared_call, preparation_count
            session.discard(prepared_call)
            prepared_call = session.prepare(message, output_limit)
            preparation_count += 1

        def above_target():
            measured = prepared_call.measurement
            threshold = measured.get("trigger_tokens")
            return (bool(threshold and measured["estimated_input_tokens"] >= threshold * .9)
                    or prepared_call.input_size > limits.max_input_estimate * .9)

        if minimal:
            message.omitted_observations = len(message.observations)
            message.observations = []
            message.cold_evidence = []
            message.cold_evidence_next_cursor = None
            original_count = len(message.evidence)
            note = {"code": "context_minimal_view", "history_omitted": query.conversation is not None,
                    "omitted_observations": message.omitted_observations, "omitted_source_count": 0,
                    "latest_operations_navigation_only": [{"call_id": o.call.call_id, "name": o.call.name, "ok": o.outcome.ok,
                                                          "error_code": o.outcome.error.code if o.outcome.error else None}
                                                         for o in state.observations[-2:]],
                    "guidance": "旧历史和部分工具观测未放入本次最小终答视图，原记录保留；不能据缺失推断源码事实，仅引用当前原文。"}
            message.observation_notes.append(note)
            rebuild()
            while (prepared_call.input_size > limits.max_input_estimate or not prepared_call.measurement.get("hard_fits", True)) and message.evidence:
                removable = evidence_manager.cooling_order(selection_query or query.question, message.evidence) if evidence_manager else message.evidence
                item = removable[0] if removable else message.evidence[0]
                message.evidence.remove(item)
                note["omitted_source_count"] = original_count - len(message.evidence)
                rebuild()
            revision = evidence_manager.revision + int([e.evidence_id for e in message.evidence] != evidence_manager.active_ids) if evidence_manager else 0
            if message.evidence_view_revision != revision:
                message.evidence_view_revision = revision
                rebuild()
            return ContextBuild(message, prepared_call, False, preparation_count, output_limit,
                                message.evidence_view_revision)

        # Only pressure permits progressively moving unpinned source out of view.
        # Source originals and catalog locations remain in state/session.
        if pressure:
            while above_target() and message.cold_evidence:
                removed = message.cold_evidence.pop()
                if evidence_manager:
                    index = next(i for i, e in enumerate(state.evidence) if e.evidence_id == removed["evidence_id"])
                    message.cold_evidence_next_cursor = min(message.cold_evidence_next_cursor if message.cold_evidence_next_cursor is not None else len(state.evidence), index)
                rebuild()
            while above_target() and message.evidence and not patch_mode:
                removable = evidence_manager.cooling_order(selection_query or query.question, message.evidence) if evidence_manager else message.evidence
                if not removable:
                    break
                message.evidence.remove(removable[0])
                if evidence_manager:
                    index = next(i for i, e in enumerate(state.evidence) if e.evidence_id == removable[0].evidence_id)
                    message.cold_evidence_next_cursor = min(message.cold_evidence_next_cursor if message.cold_evidence_next_cursor is not None else len(state.evidence), index)
                rebuild()
            # If even protected source alone exceeds capacity, send explicit
            # feedback instead of silently trimming lines or failing on 40KB.
            if prepared_call.measurement.get("pressure") and message.evidence and not patch_mode:
                saved = message.evidence
                message.evidence = []
                probe = session.prepare(message, output_limit)
                base_fits = not probe.measurement.get("pressure")
                session.discard(probe)
                message.evidence = saved
                if base_fits:
                    note = {"code": "context_capacity", "deferred_evidence_ids": [],
                            "guidance": "Protected source does not fit this request. Request fewer IDs or read a smaller complete-line range; deferred IDs are not citable."}
                    message.observation_notes.append(note)
                    while prepared_call.measurement.get("pressure") and message.evidence:
                        removed = max(message.evidence, key=lambda e: len(e.text.encode()))
                        message.evidence.remove(removed)
                        message.deferred_evidence_ids.append(removed.evidence_id)
                        note["deferred_evidence_ids"].append(removed.evidence_id)
                        message.cold_evidence_next_cursor = 0
                        rebuild()
        revision = evidence_manager.revision + int([e.evidence_id for e in message.evidence] != evidence_manager.active_ids) if evidence_manager else 0
        if message.evidence_view_revision != revision:
            message.evidence_view_revision = revision
            rebuild()
        return ContextBuild(
            message, prepared_call, can_use_tools, preparation_count, output_limit,
            revision,
        )
