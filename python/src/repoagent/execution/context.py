"""Select model-visible history and observations, then prepare the exact request.

This policy has no authority to send requests or alter the cumulative source state.
"""

from dataclasses import dataclass

from repoagent.execution.contracts import ModelRequest
from repoagent.execution.prompts import request_instruction
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
        selected = evidence_manager.select(selection_query or query.question) if evidence_manager else state.evidence
        message = ModelRequest(
            navigation_hints=navigation_hints,
            stage=stage,
            instruction=instruction,
            query=query,
            evidence=selected,
            cold_evidence=evidence_manager.catalog() if evidence_manager else [],
            repair_feedback=repair_feedback,
            stream=limits.stream and stage != "plan" and session.on_delta is not None,
            available_tools=(registry.schemas() if registry else tool_schemas(workspace=True)) if can_use_tools else [],
            observations=[
                o
                for o in state.observations
                if finalization_reason
                or not (o.call.call_id.startswith("initial-") and o.outcome.ok)
            ]
            if stage == "act"
            else [],
            remaining_model_requests=limits.max_model_requests - usage.model_requests,
            remaining_tool_calls=limits.max_tool_calls - usage.tool_calls,
        ).model_copy(deep=True)
        message.cold_evidence_next_cursor = evidence_manager.catalog_next_cursor if evidence_manager else None
        if conversation_session:
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

        # Only pressure permits progressively moving unpinned source out of view.
        # Source originals and catalog locations remain in state/session.
        if pressure:
            while prepared_call.measurement.get("pressure") and message.cold_evidence:
                removed = message.cold_evidence.pop()
                if evidence_manager:
                    index = next(i for i, e in enumerate(state.evidence) if e.evidence_id == removed["evidence_id"])
                    message.cold_evidence_next_cursor = min(message.cold_evidence_next_cursor if message.cold_evidence_next_cursor is not None else len(state.evidence), index)
                rebuild()
            while prepared_call.measurement.get("pressure") and len(message.evidence) > 1:
                removable = [e for e in message.evidence if not evidence_manager or e.evidence_id not in evidence_manager.pinned]
                if not removable:
                    break
                message.evidence.remove(removable[-1])
                if evidence_manager:
                    index = next(i for i, e in enumerate(state.evidence) if e.evidence_id == removable[-1].evidence_id)
                    message.cold_evidence_next_cursor = min(message.cold_evidence_next_cursor if message.cold_evidence_next_cursor is not None else len(state.evidence), index)
                rebuild()
        if evidence_manager:
            sent_ids = [e.evidence_id for e in message.evidence]
            if sent_ids != evidence_manager.active_ids:
                evidence_manager.revision += 1
                evidence_manager.active_ids = sent_ids
        return ContextBuild(
            message, prepared_call, can_use_tools, preparation_count, output_limit,
            evidence_manager.revision if evidence_manager else 0,
        )
