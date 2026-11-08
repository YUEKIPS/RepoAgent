"""Small step journal; facts can be persisted without enabling automatic replay."""

from repoagent.execution.control import StopExecution


class RunJournal:
    def __init__(self, run_id, trace, sink=None):
        self.run_id, self.trace, self.sink = run_id, trace, sink
        self.sequence = 0
        self.current = None

    def emit(self, kind, **fields):
        event = dict(event=kind, run_id=self.run_id, **fields)
        self.trace.append(event)
        if self.sink is not None:
            try:
                self.sink(event)
            except Exception:
                self.sink = None
                raise StopExecution("FAILED", "run_record_failed") from None

    def start(self, stage):
        self.close("completed")
        self.sequence += 1
        self.current = dict(step=self.sequence, stage=stage)
        self.emit("step_started", **self.current)

    def responded(self, kind):
        if self.current:
            self.emit("model_completed", **self.current, response_kind=kind)

    def observed(self, call, outcome):
        if self.current:
            self.emit(
                "tool_observed",
                **self.current,
                call_id=call.call_id,
                tool=call.name,
                ok=outcome.ok,
            )

    def close(self, status):
        if self.current:
            current, self.current = self.current, None
            self.emit("step_completed", **current, status=status)

    def finish(self, status):
        self.close(status)
        self.emit("run_completed", status=status, steps=self.sequence)
