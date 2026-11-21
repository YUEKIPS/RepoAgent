"""独立会话账本。事务绑定父轮次/请求；正文与UsageStore分开，恢复不执行任务。"""

import json
import re
import sqlite3
from contextlib import contextmanager

from repoagent.execution.context.history import HistoryPolicy
from repoagent.execution.contracts import (
    Workspace,
)
from repoagent.execution.conversation_session import SessionEventStore
from repoagent.storage import guard_database
from repoagent.web.errors import ServiceError


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def workspace(repository, manifest):
    return Workspace.bind({repository: manifest}).model_dump()


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ServiceError("invalid_identifier")


class ConversationStore:
    def __init__(self, path):
        self.path = path
        if path.is_symlink():
            raise ValueError("Refuse conversation database symlink")
        guard_database(path, "conversations-current-v1")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, workspace TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '新会话', head TEXT,
                    turn_count INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS turns(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, command TEXT NOT NULL,
                    job TEXT NOT NULL, context TEXT NOT NULL,
                    UNIQUE(session_id, sequence));
                CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id,sequence);
                CREATE TABLE IF NOT EXISTS run_events(
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL, event TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_turn ON run_events(turn_id,ordinal);
                CREATE TABLE IF NOT EXISTS conversation_summaries(session_id TEXT PRIMARY KEY,revision TEXT NOT NULL,body TEXT NOT NULL);
            """)
            version = db.execute("SELECT version FROM meta").fetchall()
            if not version:
                db.execute("INSERT INTO meta VALUES(1)")
            elif version != [(1,)]:
                raise ValueError("Unsupported conversation schema")
        self.event_store = SessionEventStore(self)

    @contextmanager
    def connect(self):
        # 每次操作独立连接，HTTP/工作线程不共享连接或隐式内存缓存。
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def session_row(row):
        if row is None:
            raise ServiceError("unknown_session", 404)
        ordinal, identifier, binding, title, head, count = row
        return {
            "ordinal": ordinal,
            "id": identifier,
            "workspace": json.loads(binding),
            "title": title,
            "head": head,
            "turn_count": count,
        }

    def get_session(self, identifier, db=None):
        valid_id(identifier)
        if db is None:
            with self.connect() as connection:
                return self.get_session(identifier, connection)
        return self.session_row(
            db.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
        )

    def create(self, identifier, binding):
        valid_id(identifier)
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO sessions(id,workspace) VALUES(?,?)",
                (identifier, encode(binding)),
            )
            saved = self.get_session(identifier, db)
            if saved["workspace"] != binding:
                raise ServiceError("request_id_conflict", 409)
            db.execute("INSERT OR IGNORE INTO session_organization(id) VALUES(?)", (identifier,))
            return saved

    def sessions(self, before=2**63 - 1, limit=20):
        self.page_bounds(before, limit)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM sessions WHERE ordinal<? ORDER BY ordinal DESC LIMIT ?",
                (before, limit + 1),
            ).fetchall()
        return {
            "items": [self.session_row(r) for r in rows[:limit]],
            "next_before": rows[limit - 1][0] if len(rows) > limit else None,
        }

    @staticmethod
    def page_bounds(before, limit):
        if (
            type(before) is not int
            or not 0 < before <= 2**63 - 1
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ServiceError("invalid_history_page")

    def turns(self, identifier, before=2**63 - 1, limit=20):
        self.page_bounds(before, limit)
        with self.connect() as db:
            session = self.get_session(identifier, db)
            rows = db.execute(
                "SELECT sequence,job FROM turns WHERE session_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?",
                (identifier, before, limit + 1),
            ).fetchall()
        return {
            "session": session,
            "items": [json.loads(r[1]) for r in reversed(rows[:limit])],
            "next_before": rows[limit - 1][0] if len(rows) > limit else None,
        }

    def prior(self, identifier, command):
        with self.connect() as db:
            row = db.execute(
                "SELECT command,job FROM turns WHERE id=?", (identifier,)
            ).fetchone()
        if row:
            if json.loads(row[0]) != command:
                raise ServiceError("request_id_conflict", 409)
            return json.loads(row[1])

    def get(self, identifier):
        valid_id(identifier)
        with self.connect() as db:
            row = db.execute(
                "SELECT job FROM turns WHERE id=?", (identifier,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def context(self, session, budget, limits=None):
        history = self.event_store.load(session["id"])
        latest = history.history_jobs()
        return HistoryPolicy.select(session, latest, budget, self.summary(session["id"]))

    def summary(self, session_id):
        return self.event_store.load(session_id).latest_summary()

    def save_summary_if_current(self, summary, turn_id, base_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session = self.get_session(summary.session_id, db)
            row = db.execute("SELECT revision FROM conversation_summaries WHERE session_id=?", (summary.session_id,)).fetchone()
            if row and row[0] == summary.revision and session["head"] == turn_id and session["workspace"]["version"] == summary.workspace_version:
                return
            if session["head"] != turn_id or session["workspace"]["version"] != summary.workspace_version or (row[0] if row else None) != base_revision:
                raise ValueError("Stale summary computation")
            history = self.event_store.load(summary.session_id, db)
            latest = history.latest_summary()
            if latest is None or latest.revision != summary.revision:
                if (latest.revision if latest else None) != base_revision:
                    raise ValueError("Stale session summary")
                history.apply_summary(summary, turn_id)
                self.event_store.save(history, db)
            db.execute("INSERT INTO conversation_summaries VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision,body=excluded.body", (summary.session_id, summary.revision, summary.model_dump_json()))

    def accept(self, command, job, context):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session = self.get_session(command["session_id"], db)
            if session["workspace"]["version"] != command["workspace_version"]:
                raise ServiceError("workspace_version_conflict", 409)
            if session["head"] != command["parent_turn_id"]:
                raise ServiceError("stale_session_parent", 409)
            if db.execute(
                "SELECT 1 FROM turns WHERE json_extract(job,'$.state')='running' LIMIT 1"
            ).fetchone():
                raise ServiceError("task_already_running", 409)
            sequence = session["turn_count"] + 1
            job["sequence"] = sequence
            db.execute(
                "INSERT INTO turns VALUES(?,?,?,?,?,?)",
                (
                    job["id"],
                    session["id"],
                    sequence,
                    encode(command),
                    encode(job),
                    encode(context.model_dump()),
                ),
            )
            db.execute(
                "UPDATE sessions SET head=?,turn_count=?,title=CASE WHEN turn_count=0 THEN ? ELSE title END WHERE id=?",
                (job["id"], sequence, job["question"][:70], session["id"]),
            )
            history = self.event_store.load(session["id"], db)
            if not history.surface:
                history.append("system/message", {"run_id": job["id"], "text": ""}, surface_op="append")
            history.append("run/start", {"run_id": job["id"]})
            history.append("user/message", {
                "run_id": job["id"], "question": job["question"],
                "log_text": job.get("log_text"),
            }, surface_op="append")
            self.event_store.save(history, db)

    def save(self, job):
        with self.connect() as db:
            db.execute("UPDATE turns SET job=? WHERE id=?", (encode(job), job["id"]))

    def record_progress(self, identifier, event):
        """Persist step metadata and the page projection in one local transaction."""
        progress = {
            key: event[key]
            for key in ("event", "step", "stage", "status", "steps", "tool", "ok")
            if key in event
        }
        with self.connect() as db:
            if not db.execute(
                "SELECT 1 FROM turns WHERE id=?", (identifier,)
            ).fetchone():
                raise ValueError("Unknown run")
            db.execute(
                "INSERT INTO run_events(turn_id,event) VALUES(?,?)",
                (identifier, encode(event)),
            )
            db.execute(
                "UPDATE turns SET job=json_set(job,'$.progress',json(?)) WHERE id=?",
                (encode(progress), identifier),
            )
        return progress

    def recover(self):
        # 启动器先取得目录级进程锁；在途任务一律可见中断，不推断其请求未发送。
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,job FROM turns WHERE json_extract(job,'$.state')='running'"
            ).fetchall()
            for identifier, raw in rows:
                job = json.loads(raw)
                job.update(
                    state="interrupted",
                    error={"code": "interrupted_review_usage_before_retry"},
                    artifact_saved=False,
                )
                db.execute(
                    "UPDATE turns SET job=? WHERE id=?", (encode(job), identifier)
                )
