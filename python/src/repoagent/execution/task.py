"""Shared startup for agent and single-answer baseline tasks."""

from repoagent.execution.runtime import Execution


def run_task(
    snapshot, query, model, limits, cancelled, clock, mode, on_progress, *,
    prepared, execution_options, saved,
):
    options = dict(execution_options or {})
    if saved:
        options["task_id"] = saved["task_id"]
    execution = Execution(
        snapshot, query, model, limits, cancelled, clock, mode, on_progress, **options,
    )
    if saved:
        execution.restore(saved)
    return execution.run(prepared)
