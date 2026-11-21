"""Request-level pressure loop. All summaries share the task's metered session."""

from repoagent.execution.context.summary import SummaryService
from repoagent.execution.contracts import ModelRequest
from repoagent.execution.control import StopExecution
from repoagent.execution.prompt_spec import STEP_SUMMARY


class PressureCoordinator:
    def __init__(
        self, *, task_id, query, session, history, registry, limits, usage, trace,
        checkpoint, request_summary, commit_summary,
    ):
        self.task_id = task_id
        self.query = query
        self.session = session
        self.history = history
        self.registry = registry
        self.limits = limits
        self.usage = usage
        self.trace = trace
        self.checkpoint = checkpoint
        self.request_summary = request_summary
        self.commit_summary = commit_summary

    def prepare(self, build, *, force=False):
        built = build(False)
        if not force and not built.prepared_call.measurement.get("pressure"):
            return built
        self.session.discard(built.prepared_call)
        before_projection = built.prepared_call.measurement
        history = self.history
        # Registration owns policy. Raw events remain immutable.
        runs = {event["data"]["run_id"] for event in history.events() if event["type"] == "tool/result"}
        for run in runs:
            projected = [self.registry.project(o, self.limits) for o in history.observations(run)]
            history.prune_observations(run, projected, self.registry.fingerprint)
        self.checkpoint("context_ready")
        built = build(True)
        self.trace.append({"event": "context_rebuilt", "reason": "request_pressure" if not force else "explicit_compaction",
                                "before": before_projection, "after": built.prepared_call.measurement,
                                "hot_evidence_ids": [e.evidence_id for e in built.message.evidence],
                                "deferred_evidence_ids": built.message.deferred_evidence_ids})
        while force or built.prepared_call.measurement.get("pressure"):
            force = False
            before = built.prepared_call.measurement
            self.session.discard(built.prepared_call)
            if (not self.limits.summary_enabled or
                self.limits.max_model_requests - self.usage.model_requests <= self.limits.final_request_reserve + 1):
                raise StopExecution("BUDGET_EXCEEDED", "context_no_summary_budget")
            # Summarize a whole prefix, preserving recent whole turns. Shrink only
            # the number of whole source units if the summary request itself is too big.
            summary = None
            material = None
            for count in (64, 32, 16, 8, 4, 2, 1):
                candidate = history.summary_material(self.task_id, self.limits.recent_turns, count)
                if candidate is None:
                    break
                request = SummaryService().build_request(self.query, candidate)
                prepared = self.session.prepare(request)
                if prepared.input_size <= self.session.max_input and prepared.measurement.get("hard_fits", True):
                    material = candidate
                    summary = self.request_summary(prepared, "history").summary
                    break
                self.session.discard(prepared)
            if material:
                self.commit_summary(summary)
            else:
                material = history.step_material(self.task_id)
                if not material:
                    raise StopExecution("BUDGET_EXCEEDED", "context_protected_content")
                request = ModelRequest(stage="summary", query=self.query.model_copy(update={"conversation": None}),
                    instruction=STEP_SUMMARY,
                    runtime_summary_input=material)
                prepared = self.session.prepare(request)
                if prepared.input_size > self.session.max_input or not prepared.measurement.get("hard_fits", True):
                    self.session.discard(prepared)
                    raise StopExecution("BUDGET_EXCEEDED", "context_summary_input")
                result = self.request_summary(prepared, "steps").summary
                self.commit_summary(result)
            built = build(True)
            self.trace.append({"event": "context_compacted", "before": before, "after": built.prepared_call.measurement})
            if built.prepared_call.measurement["estimated_input_tokens"] >= before["estimated_input_tokens"] and built.prepared_call.input_size >= before["input_bytes"]:
                self.session.discard(built.prepared_call)
                raise StopExecution("BUDGET_EXCEEDED", "context_no_progress")
        return built
