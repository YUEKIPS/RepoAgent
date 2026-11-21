"""Own the task slot from worker startup to durable publication."""

from threading import Event, Thread

from repoagent.application.runner import RunOutcome


class TaskLifecycle:
    def __init__(
        self, lock, conversations, checkpoints, repositories, streams, *, clean, export,
    ):
        self.lock = lock
        self.conversations = conversations
        self.checkpoints = checkpoints
        self.repositories = repositories
        self.streams = streams
        self.clean = clean
        self.export = export
        self.jobs = {}
        self.events = {}
        self.active = None
        self.storage_fault = False

    def start(self, job, execute, *, resume=False):
        """Start an admitted task while its caller holds the application lock."""
        task_id = job["id"]
        if resume:
            job = job | {"state": "running", "error": None}
            self.conversations.save(job)
        self.jobs[task_id] = job
        self.active = task_id
        try:
            event = self.events[task_id] = Event()
            Thread(target=self._run, args=(task_id, execute, event), daemon=True).start()
        except Exception:
            error = {"code": "task_start_failed"}
            if not resume:
                return self._finish(task_id, RunOutcome(None, error))
            # No worker ran. Keep the original checkpoint and allow another resume.
            job.update(state="interrupted", error=error)
            try:
                self.conversations.save(job)
            except Exception:
                self.storage_fault = True
                job["error"] = {"code": "conversation_storage_requires_review"}
            finally:
                self._release(task_id)
        return job

    def cancel(self, task_id):
        with self.lock:
            job = self.jobs.get(task_id)
            if job and job["state"] == "running":
                self.events[task_id].set()
                job["cancel_requested"] = True
                self.conversations.save(job)

    def _run(self, task_id, execute, event):
        def progress(data):
            with self.lock:
                projection = self.conversations.record_progress(task_id, data)
                self.jobs[task_id]["progress"] = projection
            self.streams.publish(task_id, data)

        try:
            outcome = execute(event.is_set, progress)
        except Exception:
            outcome = RunOutcome(None, {"code": "history_or_execution_setup_failed"})
        with self.lock:
            self._finish(task_id, outcome)

    def _finish(self, task_id, outcome):
        try:
            job = self.jobs[task_id] | {
                "state": "finished", "result": outcome.result, "error": outcome.error,
            }
            job = self.jobs[task_id] = self._publish(job)
            published = (
                job["publication_status"] == "committed"
                and (job.get("result") or {}).get("execution_status") == "SUCCEEDED"
            )
            self.streams.publish(
                task_id, {"event": "published" if published else "failed", "state": "finished"},
            )
            return job
        finally:
            self._release(task_id)

    def _release(self, task_id):
        self.events.pop(task_id, None)
        if not self.storage_fault:
            self.jobs.pop(task_id, None)
        self.active = None

    def _publish(self, job):
        job = self.clean(job)
        task_id = job["id"]
        job.update(artifact_saved=False, publication_status="committed")
        try:
            self.repositories.publication_pending(task_id)
            self.checkpoints.commit_terminal(job)
        except Exception:
            # Keep the checkpoint and repository protections for recovery. The
            # in-memory draft stays unpublished and cannot be used as a citation.
            self.storage_fault = True
            job.update(
                publication_status="uncommitted",
                uncommitted_result=job["result"],
                result=None,
                error={"code": "conversation_storage_requires_review"},
                artifact_error="conversation_storage_requires_review",
            )
            return job
        try:
            job["artifact_saved"] = True
            self.export(job)
        except Exception:
            job.update(
                artifact_saved=False, artifact_error="artifact_write_failed_export_from_browser",
            )
        try:
            self.conversations.save(job)
        except Exception:
            self.storage_fault = True
            job["artifact_error"] = "conversation_storage_requires_review"
        try:
            self.repositories.finish_run(
                task_id,
                published=(job.get("result") or {}).get("execution_status") == "SUCCEEDED",
            )
        except Exception:
            # The answer is already committed. Startup reconciles repository pins.
            self.storage_fault = True
        return job
