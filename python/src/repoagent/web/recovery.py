"""Conservative recovery decisions; never infer not-sent from a missing response."""

import time

from repoagent.execution.contracts import Query
from repoagent.execution.runtime import PROTOCOL_VERSION
from repoagent.execution.usage_store import UsageStore
from repoagent.models.base import configuration_id
from repoagent.models.factory import create_adapter


class RecoveryCoordinator:
    def __init__(self, service):
        self.service = service

    def inspect(self, job):
        saved = self.service.checkpoints.load(job["id"])
        if not saved:
            return None, "checkpoint_unavailable"
        if job.get("cancel_requested"):
            return None, "cancelled"
        if saved["execution_version"] != PROTOCOL_VERSION:
            return None, "checkpoint_version_mismatch"
        if time.time() < saved["saved_wall"] - 2:
            return None, "checkpoint_clock_changed"
        if time.time() >= saved["deadline_at"]:
            return None, "checkpoint_expired"
        if saved["phase"] == "execution_failed":
            return None, "checkpoint_execution_failed"
        if saved["phase"] == "context_compacting" and (not saved.get("pressure_pending") or (saved.get("pending_response") or {}).get("kind") != "summary"):
            return None, "context_compaction_uncommitted"
        if saved.get("extensions_fingerprint") != self.service.extensions_fingerprint:
            return None, "checkpoint_extensions_mismatch"
        if saved["phase"] == "tool_inflight":
            return None, "source_operation_unsettled"
        session = self.service.conversations.get_session(job["session_id"])
        if session["head"] != job["id"]:
            return None, "stale_session_parent"
        self.service._bound_manifest(session)
        if saved["query"]["workspace"] != session["workspace"]:
            return None, "workspace_version_conflict"
        if not job["simulated"]:
            adapter = create_adapter(self.service.model_snapshot.create_config()).with_native_thinking(job["policy"]["native_thinking"])
            if configuration_id(adapter.configuration) != saved["session"]["generation_id"]:
                return None, "checkpoint_configuration_mismatch"
            ledger = UsageStore(self.service.usage_path)
            ledger.release_reserved_task(saved["task_id"])
            usage = ledger.summary(saved["task_id"])
            if usage["unresolved_requests"] or usage["halted"] or saved["session"]["halted"]:
                return None, "checkpoint_usage_unknown"
            if usage["unique_requests"] != saved["usage"]["model_requests"]:
                return None, "checkpoint_response_unavailable"
        return saved, None

    def resume_eligible(self):
        with self.service.conversations.connect() as db:
            rows = db.execute("SELECT id FROM turns WHERE json_extract(job,'$.state')='interrupted' ORDER BY sequence").fetchall()
        for (identifier,) in rows:
            try:
                self.service.resume(identifier)
                return
            except Exception:
                continue


def restored_query(saved):
    return Query.model_validate(saved["query"])
