"""Assemble model and repository dependencies for one execution."""

import inspect
from dataclasses import dataclass

from repoagent.execution.model_session import ModelSession
from repoagent.execution.usage_store import UsageStore
from repoagent.models.base import ModelServiceError
from repoagent.models.factory import create_adapter
from repoagent.models.mock import ScriptedModel
from repoagent.repositories.github import NetworkBudget
from repoagent.repositories.sources import FetchContext, GitHubSource


@dataclass
class RunOutcome:
    result: dict | None
    error: dict | None


class TaskRunner:
    def __init__(
        self,
        model_snapshot,
        usage_path,
        mock_responses,
        snapshots,
        runners,
        adapter_factory=create_adapter,
    ):
        self.model_snapshot, self.usage_path = model_snapshot, usage_path
        self.mock_responses, self.snapshots, self.runners = (
            mock_responses,
            snapshots,
            runners,
        )
        self.adapter_factory = adapter_factory

    def execute(self, command, query, cancelled, on_progress=None, *, execution_options=None, saved=None):
        model = None
        try:
            if command.model_source == "live":
                model = ModelSession(
                    self.adapter_factory(self.model_snapshot.create_config()),
                    UsageStore(self.usage_path),
                )
            else:
                model = ScriptedModel(
                    [
                        ModelServiceError("simulated")
                        if row == {"error": "service_error"}
                        else row
                        for row in self.mock_responses
                    ]
                )
            context = FetchContext(
                command.request_id, NetworkBudget(max_seconds=30, cancelled=cancelled)
            )
            sources = {
                member.repository: (
                    self.snapshots[member.repository].for_run(context)
                    if isinstance(self.snapshots[member.repository], GitHubSource)
                    else self.snapshots[member.repository]
                )
                for member in query.workspace.repositories
            }
            if execution_options is not None:
                if saved and command.model_source == "mock":
                    model.responses = model.responses[saved["usage"]["model_requests"]:]
            runner = self.runners[command.mode]
            parameters = inspect.signature(runner).parameters
            additional = {"execution_options": execution_options, "saved": saved} if "execution_options" in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values()) else {}
            result = runner(
                sources,
                query,
                model,
                limits=command.limits,
                cancelled=cancelled,
                on_progress=on_progress,
                **additional,
            ).model_dump()
            return RunOutcome(result, None)
        except ModelServiceError as exc:
            error = {
                "code": exc.code,
                "record": exc.record.model_dump() if exc.record else None,
            }
            if model is not None:
                usage = getattr(model, "usage", None)
                error["usage"] = usage.model_dump() if usage else None
            return RunOutcome(None, error)
        except Exception:
            return RunOutcome(None, {"code": "task_setup_or_execution_error"})

    def resume(self, command, query, cancelled, saved, **kwargs):
        return self.execute(command, query, cancelled, saved=saved, **kwargs)
