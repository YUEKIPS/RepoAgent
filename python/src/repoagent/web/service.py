"""单用户本地服务：全局仓库池内固定版本的持久会话。"""

import hashlib
import json
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from repoagent import __version__
from repoagent.execution.agent import run_agent
from repoagent.execution.baseline import run_baseline
from repoagent.execution.checkpoints import CheckpointStore
from repoagent.execution.contracts import (
    ExecutionPolicy,
    Query,
    StrictModel,
    Workspace,
    origin_fields,
)
from repoagent.execution.history import HistoryPolicy
from repoagent.execution.limits import Limits
from repoagent.execution.model_session import ModelSession
from repoagent.execution.usage_store import UsageStore
from repoagent.models.base import ModelServiceError
from repoagent.models.factory import create_adapter
from repoagent.repositories.github import GitHubClient
from repoagent.repositories.sources import (
    GitHubSource,
    descriptor,
    offline_read,
)
from repoagent.repositories.store import RepositoryStore
from repoagent.repositories.text import DECODER_VERSION
from repoagent.repositories.tools import RepositoryTools
from repoagent.snapshots.repository import verify_snapshot
from repoagent.storage import DATA_VERSION
from repoagent.tools.store import SnapshotTools, read_text_block
from repoagent.web.conversations import ConversationStore, valid_id
from repoagent.web.errors import ServiceError
from repoagent.web.library import LibraryStore, logical_key
from repoagent.web.recovery import RecoveryCoordinator
from repoagent.web.repositories import RepositoryManager, alias
from repoagent.web.runner import RunOutcome, TaskRunner
from repoagent.web.streams import StreamBroker


class ChatInput(StrictModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    repository: str = Field(min_length=1, max_length=80)
    mode: Literal["agent", "baseline"]
    model_source: Literal["mock", "live"] | None = None
    question: str = Field(min_length=1, max_length=4000)
    log_text: str | None = Field(default=None, max_length=12000)
    limits: Limits = Field(default_factory=Limits)
    policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workspace_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_turn_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def binding(self):
        if bool(self.session_id) != bool(self.workspace_version) or (
            not self.session_id and self.parent_turn_id
        ):
            raise ValueError("Incomplete conversation binding")
        return self


class NewSessionInput(StrictModel):
    repositories: list[str] = Field(min_length=1, max_length=8)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ModelSourceInput(StrictModel):
    model_source: Literal["mock", "live"]


class ChatService:
    """服务器固定快照/模型/用量记录位置；浏览器可设置每任务限额，不能提交路径/密钥。

    每轮创建独立模型执行，显式带入可见会话历史。会话请求由SQLite跨重启幂等；
    单活动任务；检查点在原预算内恢复，未知发送、过期及取消均不自动续跑。
    """

    def __init__(
        self,
        snapshots,
        output_dir,
        *,
        model_snapshot=None,
        usage_path=None,
        mock_responses=None,
        secrets=(),
        initial_model_source=None,
        github_client=None,
        repository_store=None,
        extensions_config=None,
    ):
        if model_snapshot is None and mock_responses is None:
            raise ValueError("Provide live configuration or mock script")
        from repoagent.tools.extensions import ExtensionBundle, load_extensions
        self.extensions_config = load_extensions(extensions_config)
        bundle = ExtensionBundle(self.extensions_config)
        try:
            self.extensions_fingerprint = bundle.fingerprint
        finally:
            bundle.close()
        self.snapshots = {
            name: Path(path).resolve() for name, path in snapshots.items()
        }
        self.manifests = {
            name: verify_snapshot(path) for name, path in self.snapshots.items()
        }
        self.output_dir = Path(output_dir).resolve()
        self.model_snapshot = model_snapshot
        self.mock_responses = deepcopy(mock_responses)
        self.usage_path = (
            Path(usage_path).resolve()
            if usage_path
            else self.output_dir / "token-usage.sqlite3"
        )
        self.secrets = tuple(s for s in secrets if s)
        self.lock = threading.Lock()
        self.jobs = {}
        self.inputs = {}
        self.events = {}
        self.active = None
        self.storage_fault = False
        self.model_sources = (["mock"] if mock_responses is not None else []) + (
            ["live"] if model_snapshot is not None else []
        )
        self.model_source = initial_model_source or (
            "live" if model_snapshot is not None else "mock"
        )
        if self.model_source not in self.model_sources:
            raise ValueError("Initial model source is unavailable")
        self._outside_snapshots(self.output_dir)
        if model_snapshot:
            self._outside_snapshots(self.usage_path)
            config = model_snapshot.create_config()
            # 先检查密钥/身份/旧账本；构造不会调用外部模型，重启复用同一批次。
            ModelSession(create_adapter(config), UsageStore(self.usage_path))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.conversations = ConversationStore(
            self.output_dir / "conversations.sqlite3"
        )
        self.conversations.recover()
        self.checkpoints = CheckpointStore(self.conversations)
        self.streams = StreamBroker()
        self.repository_store = repository_store or RepositoryStore(
            self.output_dir / "repositories"
        )
        self.github_client = github_client or GitHubClient()
        self.repository_manager = RepositoryManager(
            self.repository_store,
            self.github_client,
            self._register_source,
            self._remove_source,
        )
        self._recover_repository_publications()
        self.library = LibraryStore(
            self.conversations, self.configuration()["repositories"]
        )
        self.repository_manager.on_result = self._organize_repository_result
        self._protect_pool_versions()
        self.recovery = RecoveryCoordinator(self)
        if self.checkpoints.auto_resume():
            self.recovery.resume_eligible()

    def _protect_pool_versions(self):
        with self.conversations.connect() as db:
            links = db.execute(
                "SELECT id,repository_key FROM pool_repositories"
            ).fetchall()
        catalog = self.configuration()["repositories"]
        for owner, key in links:
            for repo in catalog:
                if (
                    repo.get("source_kind") == "github_lazy"
                    and logical_key(repo) == key
                ):
                    self.repository_store.keep_version(
                        "pool-" + owner, repo["source_version_id"]
                    )

    def _organize_repository_result(self, command, result):
        if result.get("repository"):
            repo = next(
                r
                for r in self.configuration()["repositories"]
                if r["id"] == result["repository"]
            )
            self.library.register_result(repo)
            self._protect_pool_versions()

    def submit_repository(self, payload):
        from repoagent.web.repositories import RepositoryCommand

        command = RepositoryCommand.model_validate(payload)
        with self.lock:
            if command.action == "remove":
                raise ServiceError("use_repository_trash", 409)
            return self.repository_manager.submit(payload)

    def organization(self, kind, **filters):
        return self.library.list(kind, **filters)

    def manage_organization(self, payload):
        with self.lock:
            active = self.jobs.get(self.active, {}).get("session_id")
            with self.repository_store.connect() as db:
                repository_busy = bool(
                    db.execute(
                        "SELECT 1 FROM import_jobs WHERE state IN ('queued','running') LIMIT 1"
                    ).fetchone()
                )
            result = self.library.manage(
                payload, active, repository_busy
            )
            self._protect_pool_versions()
            return result

    def _register_source(self, source):
        with self.lock:
            prior = self.manifests.get(alias(source))
            if prior and origin_fields(prior) != origin_fields(source.descriptor()):
                raise ServiceError("repository_alias_conflict", 409)
            self.snapshots[alias(source)] = source
            self.manifests[alias(source)] = source.descriptor()

    def _remove_source(self, version):
        with self.lock:
            self.repository_store.remove_version(version)
            for name, source in list(self.snapshots.items()):
                if isinstance(source, GitHubSource) and source.version == version:
                    del self.snapshots[name]
                    del self.manifests[name]

    def _recover_repository_publications(self):
        # 先依据持久会话保留版本/结果，再处理已确认终态；未知状态保守保留。
        with self.conversations.connect() as db:
            sessions = db.execute("SELECT id,workspace FROM sessions").fetchall()
            turns = db.execute("SELECT id,job FROM turns").fetchall()
        available = {r["source_version_id"] for r in self.repository_store.versions()}
        # main已取得独占目录租约；进程重启后旧页面读取不可能仍持有内存字节。
        with self.repository_store.connect() as db:
            db.execute("DELETE FROM pins WHERE state='reading'")
            db.execute(
                "UPDATE fetch_states SET state='failed',error='interrupted_fetch' WHERE state='fetching'"
            )
        for owner, binding in sessions:
            for member in json.loads(binding)["repositories"]:
                version = member.get("source_version_id")
                if version in available:
                    self.repository_store.keep_version(owner, version)
        for owner, raw in turns:
            job = json.loads(raw)
            if job.get("state") == "finished":
                self.repository_store.finish_run(
                    owner,
                    published=bool(
                        job.get("result", {}).get("execution_status") == "SUCCEEDED"
                    )
                    if job.get("result")
                    else False,
                )

    def _outside_snapshots(self, path):
        if any(
            path.resolve().is_relative_to(root) or root.is_relative_to(path.resolve())
            for root in self.snapshots.values()
            if isinstance(root, Path)
        ):
            raise ValueError("Output and ledger must be separate from snapshots")

    def _clean(self, data):
        if isinstance(data, str):
            for secret in self.secrets:
                data = data.replace(secret, "[REDACTED]")
            return data
        if isinstance(data, dict):
            return {key: self._clean(value) for key, value in data.items()}
        if isinstance(data, list):
            return [self._clean(value) for value in data]
        return data

    def configuration(self):
        source = self.model_source
        simulated = source == "mock"
        config = self.model_snapshot.create_config() if not simulated else None
        limits = Limits()
        return {
            "release": __version__,
            "data_version": DATA_VERSION,
            "storage_namespace": DATA_VERSION + ":" + hashlib.sha256(str(self.output_dir).encode()).hexdigest()[:16],
            "auto_resume": self.checkpoints.auto_resume() if hasattr(self, "checkpoints") else False,
            "streaming": True,
            "simulated": simulated,
            "model_source": source,
            "model_sources": list(self.model_sources),
            "model": config.model if config else "scripted-mock-v1",
            "repositories": [
                {
                    "id": name,
                    "commit": m.commit,
                    "state": m.state,
                    **origin_fields(m),
                    **(
                        {
                            "name": m.source,
                            "repository_id": self.repository_store.revision(
                                m.source_version_id
                            )["repository_id"],
                            "text_profile": self.repository_store.text_profile(
                                m.source_version_id
                            ),
                            "text_decoder": self.repository_store.text_decoder(
                                m.source_version_id
                            ),
                            "coverage": descriptor(self.snapshots[name]).coverage,
                            "ref": self.repository_store.revision(m.source_version_id)[
                                "ref"
                            ],
                        }
                        if isinstance(self.snapshots[name], GitHubSource)
                        else {}
                    ),
                }
                for name, m in self.manifests.items()
            ],
            "repository_cache": self.repository_store.stats(),
            "text_decoder_version": DECODER_VERSION,
            "github_auth": self.github_client.authentication_status,
            "account_source": "API account associated with the configured local key"
            if not simulated
            else None,
            "model_capabilities": config.capabilities.model_dump() if config else None,
            "thinking_options": config.capabilities.selections()
            if config
            else ["default"],
            "single_turn": False,
            "conversation_schema": "conversation-v3",
            "repository_pool": True,
            "workspace_support": True,
            "max_workspace_repositories": 8,
            "active_task": self.active,
            "history_policy": HistoryPolicy.configuration(),
            "limits": {
                "model_requests": limits.max_model_requests,
                "tool_calls": limits.max_tool_calls,
                "seconds": limits.deadline_seconds,
                "input_estimate": limits.max_input_estimate,
                "output_estimate": limits.max_output_estimate,
                "estimation_unit": "UTF-8 JSON bytes",
            },
            "limits_schema": Limits.model_json_schema(),
            "policy_default": ExecutionPolicy().model_dump(),
        }

    def repository_directory(self, version, path="", cursor=0):
        source = next(
            (
                source
                for source in self.snapshots.values()
                if isinstance(source, GitHubSource) and source.version == version
            ),
            None,
        )
        if source is None:
            raise ServiceError("unknown_source_version", 404)
        return RepositoryTools(source).list_directory(
            path=path, cursor=cursor, limit=30
        )

    def set_model_source(self, payload):
        selection = ModelSourceInput.model_validate(payload)
        with self.lock:
            if self.active:
                raise ServiceError("task_already_running", 409)
            if selection.model_source not in self.model_sources:
                raise ServiceError("model_source_unavailable", 409)
            # 绝对赋值而非toggle：重复网络请求不会来回翻转，也不会调用模型/修改账本。
            self.model_source = selection.model_source
            return self.configuration()

    def submit(self, payload):
        return self._submit_conversation(ChatInput.model_validate(payload))

    def create_session(self, payload):
        selection = NewSessionInput.model_validate(payload)
        names = selection.repositories
        if len(set(names)) != len(names):
            raise ServiceError("invalid_workspace_selection")
        if any(name not in self.manifests for name in names):
            raise ServiceError("unknown_repository")
        binding = Workspace.bind({name: self.manifests[name] for name in names})
        with self.lock:
            catalog = self.configuration()["repositories"]
            self.library.eligible(names, catalog)
            if self.active:
                raise ServiceError("task_already_running", 409)
            with self.conversations.connect() as db:
                prior = db.execute(
                    "SELECT workspace FROM sessions WHERE id=?", (selection.request_id,)
                ).fetchone()
            if prior and json.loads(prior[0]) != binding.model_dump():
                raise ServiceError("request_id_conflict", 409)
            for member in binding.repositories:
                if member.source_kind == "github_lazy":
                    self.repository_store.keep_version(
                        selection.request_id, member.source_version_id
                    )
            return self.conversations.create(
                selection.request_id,
                binding.model_dump(),
            )

    def sessions(self, **page):
        return self._clean(self.conversations.sessions(**page))

    def turns(self, identifier, **page):
        result = self.conversations.turns(identifier, **page)
        result["session"]["organization"] = self.library.session(identifier)
        return self._clean(result)

    def _bound_manifest(self, session):
        binding = Workspace.model_validate(session["workspace"])
        for member in binding.repositories:
            manifest = self.manifests.get(member.repository)
            if (
                not manifest
                or origin_fields(manifest) != origin_fields(member)
                or (manifest.snapshot_id, manifest.commit)
                != (
                    member.snapshot_id,
                    member.commit,
                )
            ):
                raise ServiceError("workspace_snapshot_unavailable", 409)
        return self.manifests[binding.repositories[0].repository]

    def _submit_conversation(self, command):
        with self.lock:
            prior = self.conversations.get(command.request_id)
            command.model_source = command.model_source or (
                prior["model_source"] if prior else self.model_source
            )
            canonical = self._clean(command.model_dump())
            saved = self.conversations.prior(command.request_id, canonical)
            if saved:
                return self._clean(deepcopy(self.jobs.get(command.request_id, saved)))
            if (
                command.request_id in self.jobs
                or (self.output_dir / f"{command.request_id}.json").exists()
            ):
                raise ServiceError("saved_request_id_already_exists", 409)
            if self.storage_fault:
                raise ServiceError("conversation_storage_requires_review", 409)
            if self.active:
                raise ServiceError("task_already_running", 409)
            if command.model_source != self.model_source:
                raise ServiceError("model_source_changed", 409)
            session = self.conversations.get_session(command.session_id)
            self.library.session(command.session_id, writable=True)
            manifest = self._bound_manifest(session)
            if (
                command.repository
                != session["workspace"]["repositories"][0]["repository"]
            ):
                raise ServiceError("session_repository_conflict", 409)
            if command.workspace_version != session["workspace"]["version"]:
                raise ServiceError("workspace_version_conflict", 409)
            if command.parent_turn_id != session["head"]:
                raise ServiceError("stale_session_parent", 409)
            config = (
                self.model_snapshot.create_config() if self.model_snapshot else None
            )
            choices = (
                config.capabilities.selections()
                if command.model_source == "live" and config
                else ["default"]
            )
            if command.policy.native_thinking not in choices:
                raise ServiceError("unsupported_reasoning_option")
            if command.model_source == "live":
                if not config:
                    raise ServiceError("model_source_unavailable", 409)
                try:
                    for stage in ("plan", "act", "answer"):
                        config.capabilities.output_for(command.limits.output_for(stage))
                except ModelServiceError as error:
                    raise ServiceError(error.code) from None
            context = self.conversations.context(
                session, HistoryPolicy.budget(command.limits), command.limits
            )
            query = Query(
                question=self._clean(command.question),
                log_text=self._clean(command.log_text),
                policy=command.policy,
                conversation=context,
                workspace=Workspace.model_validate(session["workspace"]),
            )
            job = {
                "id": command.request_id,
                "state": "running",
                "deadline_at": (
                    time.time() + command.limits.deadline_seconds
                    if command.limits.deadline_seconds is not None else None
                ),
                "repository": command.repository,
                "mode": command.mode,
                "question": query.question,
                "log_text": query.log_text,
                "commit": manifest.commit,
                "snapshot_id": manifest.snapshot_id,
                **origin_fields(manifest),
                "session_id": command.session_id,
                "workspace_version": command.workspace_version,
                "parent_turn_id": command.parent_turn_id,
                "simulated": command.model_source == "mock",
                "model_source": command.model_source,
                "cancel_requested": False,
                "result": None,
                "error": None,
                "artifact_saved": False,
                "limits": command.limits.model_dump(),
                "policy": command.policy.model_dump(),
                "context": {
                    "included_turn_ids": [t.turn_id for t in context.turns],
                    "omitted_turns": context.omitted_turns,
                    "policy": "request-pressure-v2",
                    "summarized_turn_ids": context.summary.source_turn_ids if context.summary else [],
                    "uncovered_turns": max(0, context.omitted_turns - len(context.summary.source_turn_ids if context.summary else [])),
                },
            }
            job["workspace"] = query.workspace.model_dump()
            job["input_sha256"] = hashlib.sha256(
                json.dumps(
                    query.model_dump(), ensure_ascii=False, sort_keys=True
                ).encode()
            ).hexdigest()
            if command.model_source == "live":
                job["configuration"] = {
                    "source_sha256": self.model_snapshot.source_sha256,
                    "resolved_sha256": self.model_snapshot.resolved_sha256,
                    "resolved": json.loads(self.model_snapshot.resolved_json),
                }
            # SQLite事务先持久化完整任务身份/上下文并推进父轮次，再启动工作线程。
            self.conversations.accept(canonical, job, context)
            self.jobs[job["id"]] = job
            self.inputs[job["id"]] = canonical
            event = self.events[job["id"]] = threading.Event()
            self.active = job["id"]
            try:
                threading.Thread(
                    target=self._execute, args=(command, query, event), daemon=True
                ).start()
            except Exception:
                job.update(state="finished", error={"code": "task_start_failed"})
                job = self._persist_terminal(job)
                self.jobs[job["id"]] = job
                self.active = None
            return self._clean(deepcopy(job))

    def _execute(self, command, query, event, saved=None):
        runner = TaskRunner(
            self.model_snapshot,
            self.usage_path,
            self.mock_responses,
            self.snapshots,
            {"agent": run_agent, "baseline": run_baseline},
            adapter_factory=create_adapter,
        )

        def progress(event_data):
            with self.lock:
                projection = self.conversations.record_progress(
                    command.request_id, event_data
                )
                self.jobs[command.request_id]["progress"] = projection
            self.streams.publish(command.request_id, event_data)

        bundle = None
        try:
            from repoagent.tools.extensions import ExtensionBundle
            bundle = ExtensionBundle(self.extensions_config)
            if bundle.fingerprint != self.extensions_fingerprint:
                raise ValueError("Extension catalog changed since server startup")
            job = self.jobs[command.request_id]
            with self.conversations.connect() as db:
                cached = db.execute("SELECT revision FROM conversation_summaries WHERE session_id=?", (command.session_id,)).fetchone()
            base = [cached[0] if cached else None]
            def save_summary(summary):
                self.conversations.save_summary_if_current(summary, command.request_id, base[0])
                base[0] = summary.revision
            options = dict(
                task_id=command.request_id,
                checkpoint_sink=lambda body: self.checkpoints.save_boundary(command.request_id, body),
                stream_sink=lambda data: self.streams.publish(command.request_id, data),
                extensions=bundle,
                conversation_session=self.conversations.event_store.load(command.session_id).export() if not saved else saved["conversation_session"],
                summary_sink=save_summary,
                deadline_at=job["deadline_at"],
            )
            outcome = runner.execute(command, query, event.is_set, progress, execution_options=options, saved=saved)
        except Exception:
            outcome = RunOutcome(None, {"code": "history_or_execution_setup_failed"})
        finally:
            if bundle:
                bundle.close()
        result, error = outcome.result, outcome.error
        with self.lock:
            job = self.jobs[command.request_id]
            job.update(state="finished", result=result, error=error)
            self.jobs[command.request_id] = self._persist_terminal(job)
            committed = self.jobs[command.request_id]
            if committed.get("publication_status") == "committed":
                self.streams.publish(command.request_id, {"event": "published" if (committed.get("result") or {}).get("execution_status") == "SUCCEEDED" else "failed", "state": "finished"})
            self.active = None
            # 会话历史分页读库，终态不常驻内存。
            if command.session_id and not self.storage_fault:
                self.jobs.pop(command.request_id, None)
                self.inputs.pop(command.request_id, None)
                self.events.pop(command.request_id, None)

    def runtime_settings(self, payload):
        if set(payload) != {"auto_resume"} or type(payload["auto_resume"]) is not bool:
            raise ServiceError("invalid_runtime_settings")
        with self.lock:
            if self.active:
                raise ServiceError("task_already_running", 409)
            return {"auto_resume": self.checkpoints.auto_resume(payload["auto_resume"])}

    def resume(self, identifier):
        valid_id(identifier)
        with self.lock:
            if self.active or self.storage_fault:
                raise ServiceError("task_already_running", 409)
            job = self.conversations.get(identifier)
            if not job or job["state"] != "interrupted":
                raise ServiceError("task_not_resumable", 409)
            try:
                saved, reason = self.recovery.inspect(job)
            except Exception:
                raise ServiceError("checkpoint_integrity_or_source_unavailable", 409) from None
            if reason:
                raise ServiceError(reason, 409)
            with self.conversations.connect() as db:
                raw = db.execute("SELECT command FROM turns WHERE id=?", (identifier,)).fetchone()[0]
            command = ChatInput.model_validate_json(raw)
            query = Query.model_validate(saved["query"])
            self.library.session(command.session_id, writable=True)
            job.update(state="running", error=None)
            self.conversations.save(job)
            self.jobs[identifier] = job
            event = self.events[identifier] = threading.Event()
            self.active = identifier
            threading.Thread(target=self._execute, args=(command, query, event, saved), daemon=True).start()
            return self._clean(deepcopy(job))

    def _persist_terminal(self, job):
        """调用方持锁；启动失败与执行完成共用终态落盘，不回显底层异常。"""
        if not job["simulated"]:
            snapshot = self.model_snapshot
            job["configuration"] = {
                "source_sha256": snapshot.source_sha256,
                "resolved_sha256": snapshot.resolved_sha256,
                "resolved": json.loads(snapshot.resolved_json),
            }
        job = self._clean(job)
        self.repository_store.publication_pending(job["id"])
        job["artifact_saved"] = False
        job["publication_status"] = "committed"
        # The database is authoritative. A candidate must not become a formal
        # answer through polling or JSON export before this transaction succeeds.
        try:
            self.checkpoints.commit_terminal(job)
        except Exception:
            self.storage_fault = True
            job["publication_status"] = "uncommitted"
            job["uncommitted_result"] = job.get("result")
            job["result"] = None
            job["error"] = {"code": "conversation_storage_requires_review"}
            job["artifact_error"] = "conversation_storage_requires_review"
            return job
        try:
            self._outside_snapshots(self.output_dir)
            target = self.output_dir / f"{job['id']}.json"
            if target.is_symlink():
                raise ValueError("Refuse artifact symlink")
            job["artifact_saved"] = True
            temporary = target.with_suffix(".tmp")
            with temporary.open("x", encoding="utf-8") as out:
                json.dump(job, out, ensure_ascii=False, indent=2)
                out.write("\n")
            temporary.replace(target)
        except Exception:
            job["artifact_saved"] = False
            job["artifact_error"] = "artifact_write_failed_export_from_browser"
        try:
            self.conversations.save(job)  # Export metadata; never reopens generation.
        except Exception:
            self.storage_fault = True
            job["artifact_error"] = "conversation_storage_requires_review"
        self.repository_store.finish_run(
            job["id"],
            published=bool((job.get("result") or {}).get("execution_status") == "SUCCEEDED"),
        )
        return job

    def get(self, task_id):
        valid_id(task_id)
        with self.lock:
            if task_id not in self.jobs:
                saved = self.conversations.get(task_id)
                if saved:
                    return self._clean(saved)
                raise ServiceError("unknown_task", 404)
            return self._clean(deepcopy(self.jobs[task_id]))

    def list_jobs(self, *, summary=False):
        with self.lock:
            if summary:
                return [
                    {
                        key: job.get(key)
                        for key in (
                            "id",
                            "state",
                            "cancel_requested",
                            "artifact_saved",
                            "artifact_error",
                        )
                    }
                    for job in self.jobs.values()
                ]
            return self._clean([deepcopy(job) for job in self.jobs.values()])

    def cancel(self, task_id):
        with self.lock:
            if task_id not in self.jobs:
                # 已持久化终态不再执行，幂等取消由get返回。
                pass
            elif self.jobs[task_id]["state"] == "running":
                self.events[task_id].set()
                self.jobs[task_id]["cancel_requested"] = True
                self.conversations.save(self.jobs[task_id])
        return self.get(task_id)

    def source(self, task_id, citation_index, start=None, column=0):
        job = self.get(task_id)
        result = job.get("result") or {}
        citations = result.get("citations", [])
        if not 0 <= citation_index < len(citations):
            raise ServiceError("unknown_citation", 404)
        c = citations[citation_index]
        repository = c["repo_id"]
        if job["workspace"]:
            if result.get("workspace") != job["workspace"]:
                raise ServiceError("workspace_snapshot_unavailable", 409)
            binding = Workspace.model_validate(job["workspace"])
            member = next(
                (m for m in binding.repositories if m.repository == repository), None
            )
            if (
                not member
                or origin_fields(c) != origin_fields(member)
                or (c.get("snapshot_id"), c["commit"])
                != (
                    member.snapshot_id,
                    member.commit,
                )
            ):
                raise ServiceError("workspace_snapshot_unavailable", 409)
            expected = origin_fields(member)
        manifest = self.manifests.get(repository)
        if (
            not manifest
            or manifest.commit != c["commit"]
            or origin_fields(manifest) != expected
        ):
            raise ServiceError("workspace_snapshot_unavailable", 409)
        start = c["start_line"] if start is None else start
        if not c["start_line"] <= start <= c["end_line"]:
            raise ServiceError("source_cursor_outside_citation")
        source = self.snapshots[repository]
        if isinstance(source, GitHubSource):
            return self._clean(
                read_text_block(
                    origin_fields(manifest) | {"commit": manifest.commit},
                    c["path"],
                    source.store.text_view(
                        source.version, c["path"], offline_read(source, c["path"])
                    ).text,
                    start,
                    c["end_line"],
                    column,
                )
                | {"repo_id": repository}
            )
        # 只读取该任务已有引用；游标无法切换文件/仓库或越过引用结束行。
        return self._clean(
            SnapshotTools(self.snapshots[repository]).read_file(
                c["path"], start, c["end_line"], column
            )
            | {"repo_id": repository}
        )
