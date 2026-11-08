"""持久仓库任务：显式添加/检查更新/移除，恢复不重发网络或模型请求。"""

import json
import threading
from typing import Literal
from urllib.parse import quote

from pydantic import Field

from repoagent.execution.contracts import StrictModel
from repoagent.repositories.github import (
    OID,
    GitHubError,
    NetworkBudget,
    repository_name,
    verify_blob,
)
from repoagent.repositories.sources import GitHubSource, prepare_github
from repoagent.repositories.store import encode
from repoagent.repositories.text import decode_text
from repoagent.web.errors import ServiceError


class RepositoryCommand(StrictModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    action: Literal[
        "add",
        "check",
        "update",
        "remove",
        "preview_encoding",
        "apply_encoding",
    ] = "add"
    commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    url: str | None = Field(default=None, max_length=512)
    ref: str = Field(default="", max_length=200)
    path: str | None = Field(default=None, max_length=1024)
    encoding: Literal["auto", "utf-8", "utf-16", "gbk", "gb18030"] | None = None
    source_version_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def alias(source):
    d = source.descriptor()
    return f"gh_{d.repository_id}_{d.source_version_id[:16]}"


class RepositoryManager:
    def __init__(self, store, client, register, unregister):
        self.on_result = None
        self.store, self.client = store, client
        self.register, self.unregister = register, unregister
        self.lock = threading.Lock()
        self.events = {}
        with store.connect() as db:
            db.execute(
                "UPDATE import_jobs SET state='interrupted' WHERE state IN ('queued','running')"
            )
        for revision in store.versions():
            register(GitHubSource(store, revision["source_version_id"], client))

    def get(self, identifier):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT state,result FROM import_jobs WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise ServiceError("unknown_repository_job", 404)
        return {
            "id": identifier,
            "state": row[0],
            "result": json.loads(row[1]) if row[1] else None,
        }

    def submit(self, payload):
        command = RepositoryCommand.model_validate(payload)
        if command.action == "add":
            if not command.url or command.source_version_id:
                raise ServiceError("repository_url_required")
            repository_name(command.url)  # 验证后才持久化，拒绝带凭据或查询的URL。
            if command.ref and (
                any(ord(c) < 33 or c in "~^:?*[\\" for c in command.ref)
                or ".." in command.ref
                or "@{" in command.ref
            ):
                raise ServiceError("invalid_repository_ref")
        elif not command.source_version_id or command.url or command.ref:
            raise ServiceError("source_version_required")
        if (command.action == "update") != (command.commit is not None):
            raise ServiceError("update_requires_fixed_commit")
        if command.action in {"preview_encoding", "apply_encoding"}:
            if not command.path or command.encoding is None:
                raise ServiceError("encoding_file_required")
            self.store.entry(command.source_version_id, command.path)
        elif command.path is not None or command.encoding is not None:
            raise ServiceError("unexpected_encoding_configuration")
        canonical = encode(
            command.model_dump(
                exclude=({"path", "encoding"} if command.path is None else set())
            )
        )
        with self.lock, self.store.connect() as db:
            prior = db.execute(
                "SELECT command FROM import_jobs WHERE id=?", (command.request_id,)
            ).fetchone()
            if prior:
                if prior != (canonical,):
                    raise ServiceError("repository_request_conflict", 409)
                return self.get(command.request_id)
            self.store.guard_space(len(canonical.encode()))
            db.execute(
                "INSERT INTO import_jobs VALUES(?,?,'queued',NULL)",
                (command.request_id, canonical),
            )
        event = self.events[command.request_id] = threading.Event()
        threading.Thread(
            target=self._execute, args=(command, event), daemon=True
        ).start()
        return self.get(command.request_id)

    def cancel(self, identifier):
        if identifier in self.events:
            self.events[identifier].set()
        return self.get(identifier)

    def _execute(self, command, event):
        acquired = False
        budget = NetworkBudget(max_requests=10, max_seconds=45, cancelled=event.is_set)
        result, state = {}, "finished"
        try:
            while not acquired:
                budget.check()
                acquired = self.store.fetch_lock.acquire(timeout=0.1)
            with self.store.connect() as db:
                db.execute(
                    "UPDATE import_jobs SET state='running' WHERE id=?",
                    (command.request_id,),
                )
            if command.action == "add":
                source, _ = prepare_github(
                    self.store, self.client, command.url, command.ref, budget=budget
                )
                self.register(source)
                result = {
                    "repository": alias(source),
                    "source_version_id": source.version,
                    "commit": source.descriptor().commit,
                    "state": "catalog_ready",
                }
            elif command.action == "update":
                revision = self.store.revision(command.source_version_id)
                source, _ = prepare_github(
                    self.store,
                    self.client,
                    "https://github.com/" + revision["name"],
                    command.commit,
                    budget=budget,
                    expected_id=revision["repository_id"],
                    tracking_ref=revision["ref"],
                    text_profile=self.store.text_profile(command.source_version_id),
                )
                self.register(source)
                result = {
                    "repository": alias(source),
                    "source_version_id": source.version,
                    "commit": source.descriptor().commit,
                    "state": "catalog_ready",
                }
            elif command.action == "apply_encoding":
                version = self.store.reinterpret(
                    command.source_version_id,
                    {command.path: command.encoding},
                )
                source = GitHubSource(self.store, version, self.client)
                self.register(source)
                result = {
                    "repository": alias(source),
                    "source_version_id": version,
                    "commit": source.descriptor().commit,
                    "state": "encoding_ready",
                }
            elif command.action == "preview_encoding":
                version, path, owner = (
                    command.source_version_id,
                    command.path,
                    "preview-" + command.request_id,
                )
                try:
                    raw = self.store.cached(version, path, owner, reuse=True)
                    if raw is None:
                        revision, entry = (
                            self.store.revision(version),
                            self.store.entry(version, path),
                        )
                        self.store.reserve(owner, entry["size"])
                        self.client.public_repository(
                            revision["name"], budget, revision["repository_id"]
                        )
                        raw = self.client.get(
                            f"/repos/{revision['name']}/git/blobs/{entry['sha']}",
                            budget,
                            raw=True,
                        )
                        verify_blob(raw, entry)
                        self.store.put(version, path, raw, owner)
                        with self.store.connect() as db:
                            db.execute(
                                "UPDATE materializations SET parse_state='raw_only' WHERE revision=? AND path=? AND parse_state='cached'",
                                (version, path),
                            )
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE materializations SET parse_state='raw_only' WHERE revision=? AND path=? AND parse_state='cached'",
                            (version, path),
                        )
                    decoded = decode_text(raw, command.encoding)
                    result = {
                        "state": "encoding_preview",
                        "path": path,
                        "encoding": command.encoding,
                        "selected_encoding": decoded.encoding,
                        "source_version_id": version,
                        "text": decoded.text[:12000],
                        "truncated": len(decoded.text) > 12000,
                    }
                finally:
                    self.store.finish_run(owner, published=False)
            elif command.action == "check":
                revision = self.store.revision(command.source_version_id)
                meta = self.client.public_repository(
                    revision["name"], budget, revision["repository_id"]
                )
                detail = self.client.get(
                    f"/repos/{meta['full_name']}/commits?sha={quote(revision['ref'], safe='')}&per_page=1",
                    budget,
                )
                commit = detail.get("sha")
                if not isinstance(commit, str) or not OID.fullmatch(commit):
                    raise GitHubError("commit_identity_failed")
                result = {
                    "changed": commit != revision["commit"],
                    "old_commit": revision["commit"],
                    "new_commit": commit,
                    "url": "https://github.com/" + revision["name"],
                    "ref": revision["ref"],
                    "source_version_id": revision["source_version_id"],
                }
            else:
                self.unregister(command.source_version_id)
                result = {"removed": command.source_version_id}
            if self.on_result:
                self.on_result(command, result)
        except GitHubError as error:
            result, state = (
                {"error": error.code},
                "cancelled" if error.code == "cancelled" else "failed",
            )
        except ServiceError as error:
            result, state = {"error": error.code}, "failed"
        except Exception:
            result, state = {"error": "repository_storage_or_task_failure"}, "failed"
        finally:
            result["network"] = {
                "requests": budget.requests,
                "bytes": budget.bytes,
                "seconds": round(budget.seconds, 4),
                "events": budget.events,
            }
            with self.store.connect() as db:
                db.execute(
                    "UPDATE import_jobs SET state=?,result=? WHERE id=?",
                    (state, encode(result), command.request_id),
                )
            self.events.pop(command.request_id, None)
            if acquired:
                self.store.fetch_lock.release()
