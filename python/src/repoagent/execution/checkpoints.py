"""Versioned durable execution boundaries in the conversation transaction domain."""

import hashlib
import json
import time

from repoagent.execution.control import StopExecution
from repoagent.execution.conversation_session import ConversationSession

CHECKPOINT_VERSION = "execution-checkpoint-v8-bounded-review"


class CheckpointStore:
    def __init__(self, conversations):
        self.conversations = conversations
        with conversations.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS checkpoints(turn_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS runtime_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")

    def save_boundary(self, turn_id, body):
        body = dict(body, checkpoint_version=CHECKPOINT_VERSION, saved_at=time.time())
        try:
            with self.conversations.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT job,session_id FROM turns WHERE id=?", (turn_id,)).fetchone()
                if not row or json.loads(row[0])["state"] != "running":
                    raise ValueError("Run is not writable")
                history = ConversationSession.restore(body["conversation_session"])
                if history.session_id != row[1] or body["state"]["applied_event_seq"] != history.seq:
                    raise ValueError("Checkpoint session binding mismatch")
                self.conversations.event_store.save(history, db)
                body.pop("conversation_session")
                body["conversation_session_ref"] = self.session_reference(history)
                raw = json.dumps(body, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(raw.encode()).hexdigest()
                db.execute("INSERT INTO checkpoints VALUES(?,1,?,?) ON CONFLICT(turn_id) DO UPDATE SET revision=revision+1,body=excluded.body,digest=excluded.digest", (turn_id, raw, digest))
                db.execute("INSERT INTO run_events(turn_id,event) VALUES(?,?)", (turn_id, json.dumps({"event": "checkpoint_saved", "phase": body["phase"]})))
        except Exception:
            raise StopExecution("FAILED", "checkpoint_write_failed") from None

    def load(self, turn_id):
        with self.conversations.connect() as db:
            row = db.execute("SELECT body,digest FROM checkpoints WHERE turn_id=?", (turn_id,)).fetchone()
        if not row:
            return None
        raw, digest = row
        if hashlib.sha256(raw.encode()).hexdigest() != digest:
            raise ValueError("Checkpoint integrity failure")
        body = json.loads(raw)
        if body.get("checkpoint_version") != CHECKPOINT_VERSION:
            raise ValueError("Unsupported checkpoint version")
        reference = body["conversation_session_ref"]
        current = self.conversations.event_store.load(reference["session_id"], event_count=reference["event_count"])
        if body["state"]["applied_event_seq"] != current.seq or reference != self.session_reference(current):
            raise ValueError("Checkpoint event prefix mismatch")
        body["conversation_session"] = current.export()
        return body

    @staticmethod
    def session_reference(history):
        return {"session_id": history.session_id, "workspace_version": history.workspace_version,
                "event_count": history.seq, "revision": history.revision}

    def auto_resume(self, enabled=None):
        with self.conversations.connect() as db:
            if enabled is not None:
                if type(enabled) is not bool:
                    raise ValueError("Expected boolean")
                db.execute("INSERT INTO runtime_settings VALUES('auto_resume',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(enabled),))
            row = db.execute("SELECT value FROM runtime_settings WHERE key='auto_resume'").fetchone()
        return bool(row and json.loads(row[0]))

    def commit_terminal(self, job):
        with self.conversations.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT job FROM turns WHERE id=?", (job["id"],)).fetchone()
            if not current:
                raise ValueError("Unknown run")
            if json.loads(current[0])["state"] == "finished":
                return
            history = self.conversations.event_store.load(job["session_id"], db)
            if not any(e["type"] == "run/end" and e["data"]["run_id"] == job["id"] for e in history.events()):
                history.append("run/end", {"run_id": job["id"], "status": (job.get("result") or {}).get("execution_status", "FAILED")})
            history.append("answer/committed", {
                "run_id": job["id"], "result": job.get("result"), "error": job.get("error"),
            })
            self.conversations.event_store.save(history, db)
            db.execute("UPDATE turns SET job=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job["id"]))
            row = db.execute("SELECT body FROM checkpoints WHERE turn_id=?", (job["id"],)).fetchone()
            if row:
                body = json.loads(row[0])
                body["phase"] = "terminal"
                body["conversation_session_ref"] = self.session_reference(history)
                body["state"]["applied_event_seq"] = history.seq
                raw = json.dumps(body, ensure_ascii=False, sort_keys=True)
                db.execute("UPDATE checkpoints SET body=?,digest=?,revision=revision+1 WHERE turn_id=?", (raw, hashlib.sha256(raw.encode()).hexdigest(), job["id"]))
            db.execute("INSERT INTO run_events(turn_id,event) VALUES(?,?)", (job["id"], json.dumps({"event": "result_committed", "state": "finished"})))
