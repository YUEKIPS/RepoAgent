"""Request-level pressure loop. All summaries share the task's metered session."""

from repoagent.execution.contracts import ModelRequest
from repoagent.execution.control import StopExecution
from repoagent.execution.prompt_spec import STEP_SUMMARY
from repoagent.execution.summary import SummaryService
from repoagent.models.base import ModelServiceError


class PressureCoordinator:
    def prepare(self, execution, build, *, force=False):
        try:
            return self._prepare(execution, build, force=force)
        except (StopExecution, ModelServiceError) as error:
            reason = error.reason if isinstance(error, StopExecution) else error.code
            recoverable = {"context_no_summary_budget", "context_protected_content", "context_summary_input", "context_no_progress", "invalid_summary_provenance"}
            if reason not in recoverable or execution.minimal_context or execution.repair_feedback:
                raise
            execution.guard()
            execution.session.ensure_healthy()
            execution.session.reset_protocol()
            execution.minimal_context = True
            execution.isolated_finalization = True
            execution.pressure_pending = None
            execution.pending_response = None
            execution.stop_retrieval(reason)
            execution.recovery_events.append({"stage": "context", "reason": reason, "action": "minimal_final_view"})
            built = build(True)
            if built.prepared_call.input_size > execution.session.max_input or not built.prepared_call.measurement.get("hard_fits", True):
                execution.session.discard(built.prepared_call)
                raise StopExecution("BUDGET_EXCEEDED", "context_protected_content") from None
            execution.trace.append({"event": "minimal_final_context", "reason": reason,
                                    "evidence_ids": [e.evidence_id for e in built.message.evidence]})
            execution.checkpoint("context_ready")
            return built

    def _prepare(self, execution, build, *, force=False):
        if execution.pressure_pending:
            if not execution.pending_response or execution.pending_response.kind != "summary":
                raise ModelServiceError(code="context_summary_response_unavailable")
            self._commit(execution, execution.pending_response.summary)
        built = build(False)
        if not force and not built.prepared_call.measurement.get("pressure"):
            return built
        execution.session.discard(built.prepared_call)
        before_projection = built.prepared_call.measurement
        history = execution.conversation_session
        # Registration owns policy. Raw events remain immutable.
        runs = {event["data"]["run_id"] for event in history.events() if event["type"] == "tool/result"}
        for run in runs:
            projected = [execution.registry.project(o, execution.limits) for o in history.observations(run)]
            history.prune_observations(run, projected, execution.registry.fingerprint)
        execution.checkpoint("context_ready")
        built = build(True)
        execution.trace.append({"event": "context_rebuilt", "reason": "request_pressure" if not force else "explicit_compaction",
                                "before": before_projection, "after": built.prepared_call.measurement,
                                "hot_evidence_ids": [e.evidence_id for e in built.message.evidence],
                                "deferred_evidence_ids": built.message.deferred_evidence_ids})
        while force or built.prepared_call.measurement.get("pressure"):
            force = False
            before = built.prepared_call.measurement
            execution.session.discard(built.prepared_call)
            if (not execution.limits.summary_enabled or
                execution.limits.max_model_requests - execution.usage.model_requests <= execution.limits.final_request_reserve + 1):
                raise StopExecution("BUDGET_EXCEEDED", "context_no_summary_budget")
            # Summarize a whole prefix, preserving recent whole turns. Shrink only
            # the number of whole source units if the summary request itself is too big.
            summary = None
            material = None
            for count in (64, 32, 16, 8, 4, 2, 1):
                candidate = history.summary_material(execution.task_id, execution.limits.recent_turns, count)
                if candidate is None:
                    break
                request = SummaryService().build_request(execution.query, candidate)
                prepared = execution.session.prepare(request)
                if prepared.input_size <= execution.session.max_input and prepared.measurement.get("hard_fits", True):
                    material = candidate
                    summary = self._invoke(execution, prepared, "history", candidate).summary
                    break
                execution.session.discard(prepared)
            if material:
                self._commit(execution, summary)
            else:
                material = history.step_material(execution.task_id)
                if not material:
                    raise StopExecution("BUDGET_EXCEEDED", "context_protected_content")
                request = ModelRequest(stage="summary", query=execution.query.model_copy(update={"conversation": None}),
                    instruction=STEP_SUMMARY,
                    runtime_summary_input=material)
                prepared = execution.session.prepare(request)
                if prepared.input_size > execution.session.max_input or not prepared.measurement.get("hard_fits", True):
                    execution.session.discard(prepared)
                    raise StopExecution("BUDGET_EXCEEDED", "context_summary_input")
                result = self._invoke(execution, prepared, "steps", material).summary
                self._commit(execution, result)
            built = build(True)
            execution.trace.append({"event": "context_compacted", "before": before, "after": built.prepared_call.measurement})
            if built.prepared_call.measurement["estimated_input_tokens"] >= before["estimated_input_tokens"] and built.prepared_call.input_size >= before["input_bytes"]:
                execution.session.discard(built.prepared_call)
                raise StopExecution("BUDGET_EXCEEDED", "context_no_progress")
        return built

    def _commit(self, execution, result):
        pending = execution.pressure_pending
        material = pending["material"]
        history = execution.conversation_session
        record = None
        if pending["kind"] == "history":
            record = SummaryService().validate_result(result, execution.query, material, execution.session.generation_id)
            history.apply_summary(record, execution.task_id)
            context = execution.query.conversation
            context.summary = record
            context.turns = [t for t in context.turns if t.turn_id not in record.source_turn_ids]
            context.omitted_turns = len(record.source_turn_ids)
            execution.trace.append({"event": "history_summarized", "summary_revision": record.revision, "source_turn_ids": record.source_turn_ids})
        elif pending["kind"] == "steps":
            if not result.items or any(set(item.turn_ids) != {execution.task_id} for item in result.items):
                raise ModelServiceError(code="invalid_summary_provenance")
            ids = history.apply_step_memory(execution.task_id, material, result, execution.session.generation_id, execution.registry.fingerprint)
            execution.session.rebase_continuation(ids)
            execution.trace.append({"event": "steps_summarized", "source_event_seqs": material["source_event_seqs"]})
        else:
            raise ModelServiceError(code="invalid_summary_provenance")
        execution.pending_response = None
        execution.pressure_pending = None
        execution.checkpoint("context_ready")
        if record and execution.summary_sink:
            execution.summary_sink(record)

    def _invoke(self, execution, prepared, kind, material):
        request = ModelRequest.model_validate_json(prepared.request_json)
        material = request.summary_input.model_dump() if kind == "history" else request.runtime_summary_input
        execution.pressure_pending = {"kind": kind, "material": material}
        execution.context_compacting = True
        execution.journal.start("summary")
        execution.trace.append({"event": "summary_request_meter", "measurement": prepared.measurement})
        previous = len(execution.session.records)
        try:
            response = execution.session.invoke(prepared).response
            execution.journal.responded("summary")
            execution.journal.close("completed")
            return response
        finally:
            execution.context_compacting = False
            for record in execution.session.records[previous:]:
                execution.trace.append({"event": "model_usage", "record": record.model_dump()})
            execution.refresh_usage()
