"""Global repository pool and conversation states; no project indirection."""

import hashlib
import json
import sqlite3
from typing import Literal

from pydantic import Field

from repoagent.execution.contracts import StrictModel
from repoagent.web.conversations import encode, valid_id
from repoagent.web.errors import ServiceError

STATES = {"active", "archived", "trashed"}


class ManagementCommand(StrictModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: Literal["session", "repository"]
    action: Literal["rename", "archive", "unarchive", "trash", "restore"]
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: int = Field(default=0, ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=100)


def logical_key(repo):
    if repo.get("source_kind") == "github_lazy":
        return "github:" + str(repo["repository_id"])
    return "local:" + repo["id"]


class LibraryStore:
    def __init__(self, conversations, catalog):
        self.conversations = conversations
        with conversations.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS session_organization(
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL, title TEXT,
                    state TEXT NOT NULL DEFAULT 'active', previous_state TEXT,
                    revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS pool_repositories(
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL, repository_key TEXT UNIQUE NOT NULL,
                    title TEXT, state TEXT NOT NULL DEFAULT 'active',
                    previous_state TEXT, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS management_requests(
                    id TEXT PRIMARY KEY, command TEXT NOT NULL, result TEXT NOT NULL);
            """)
            for repo in catalog:
                self._register(db, repo)

    @staticmethod
    def _row(db, table, identifier):
        valid_id(identifier)
        db.row_factory = sqlite3.Row
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ServiceError("unknown_organization_item", 404)
        return dict(row)

    @staticmethod
    def _register(db, repo):
        key = logical_key(repo)
        identifier = hashlib.sha256(key.encode()).hexdigest()[:32]
        # Reimporting must not silently restore an archived or trashed repository.
        db.execute(
            "INSERT OR IGNORE INTO pool_repositories(id,repository_key) VALUES(?,?)",
            (identifier, key),
        )
        return identifier

    def register_result(self, repo):
        with self.conversations.connect() as db:
            self._register(db, repo)

    def session(self, identifier, writable=False):
        with self.conversations.connect() as db:
            row = self._row(db, "session_organization", identifier)
            row["read_only"] = row["state"] != "active"
            if writable and row["read_only"]:
                raise ServiceError("session_read_only", 409)
            return row

    def eligible(self, names, catalog):
        lookup = {r["id"]: r for r in catalog}
        with self.conversations.connect() as db:
            allowed = {
                r[0] for r in db.execute(
                    "SELECT repository_key FROM pool_repositories WHERE state='active'"
                )
            }
        if any(
            n not in lookup or logical_key(lookup[n]) not in allowed
            or lookup[n]["state"] not in {"READY", "PARTIAL"}
            for n in names
        ):
            raise ServiceError("repository_not_in_pool", 409)

    def list(self, kind, *, state="active", before=2**63 - 1, limit=20):
        if kind not in {"session", "repository"} or state not in STATES:
            raise ServiceError("invalid_organization_filter")
        self.conversations.page_bounds(before, limit)
        table = "session_organization" if kind == "session" else "pool_repositories"
        with self.conversations.connect() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(
                f"SELECT * FROM {table} WHERE state=? AND ordinal<? ORDER BY ordinal DESC LIMIT ?",
                (state, before, limit + 1),
            )]
            if kind == "session":
                for row in rows:
                    original = self.conversations.get_session(row["id"], db)
                    row["title"] = row["title"] or original["title"]
                    row["turn_count"] = original["turn_count"]
                    row["workspace"] = original["workspace"]
        return {
            "items": rows[:limit],
            "next_before": rows[limit - 1]["ordinal"] if len(rows) > limit else None,
        }

    def manage(self, payload, active_session=None, repository_busy=False):
        cmd = ManagementCommand.model_validate(payload)
        canonical = encode(cmd.model_dump())
        table = "session_organization" if cmd.kind == "session" else "pool_repositories"
        with self.conversations.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT command,result FROM management_requests WHERE id=?",
                (cmd.request_id,),
            ).fetchone()
            if prior:
                if prior[0] != canonical:
                    raise ServiceError("request_id_conflict", 409)
                return json.loads(prior[1])
            if (cmd.action == "rename") != (cmd.title is not None) or (
                cmd.title is not None and (
                    not cmd.title.strip() or any(ord(c) < 32 for c in cmd.title)
                )
            ):
                raise ServiceError("invalid_organization_title")
            item = self._row(db, table, cmd.id)
            if item["revision"] != cmd.revision:
                raise ServiceError("organization_conflict", 409)
            if cmd.action != "rename" and (
                cmd.kind == "session" and cmd.id == active_session
                or cmd.kind == "repository" and (repository_busy or active_session)
            ):
                raise ServiceError("organization_task_running", 409)
            if cmd.action == "rename":
                db.execute(
                    f"UPDATE {table} SET title=?,revision=revision+1 WHERE id=?",
                    (cmd.title.strip(), cmd.id),
                )
            else:
                transitions = {
                    "archive": ("active", "archived"),
                    "unarchive": ("archived", "active"),
                    "trash": (None, "trashed"),
                    "restore": ("trashed", item["previous_state"] or "active"),
                }
                expected, target = transitions[cmd.action]
                if item["state"] == "trashed" and cmd.action != "restore" or (
                    expected and item["state"] != expected
                ):
                    raise ServiceError("invalid_organization_transition", 409)
                previous = item["state"] if cmd.action == "trash" else None
                db.execute(
                    f"UPDATE {table} SET state=?,previous_state=?,revision=revision+1 WHERE id=?",
                    (target, previous, cmd.id),
                )
            result = self._row(db, table, cmd.id)
            db.execute(
                "INSERT INTO management_requests VALUES(?,?,?)",
                (cmd.request_id, canonical, encode(result)),
            )
            return result
