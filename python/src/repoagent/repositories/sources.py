"""版本来源只读契约与组合：本地快照不迁移，GitHub正文按需取得。"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

from repoagent.repositories.github import (
    OID,
    GitHubClient,
    GitHubError,
    NetworkBudget,
    repository_name,
    verify_blob,
    verify_catalog,
)
from repoagent.repositories.store import parser_versions
from repoagent.repositories.text import TEXT_ERRORS, profile
from repoagent.snapshots.repository import contained, verify_snapshot


class RepositorySource(Protocol):
    def descriptor(self): ...
    def read_bytes(self, path: str) -> bytes: ...


@dataclass(frozen=True)
class LocalSnapshotSource:
    root: Path

    def descriptor(self):
        return verify_snapshot(self.root)

    def read_bytes(self, path):
        manifest = self.descriptor()
        entry = next((e for e in manifest.files if e.path == path and e.sha256), None)
        if entry is None:
            raise GitHubError("file_outside_readable_catalog")
        data = contained(self.root, "content/" + path).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise GitHubError("local_content_integrity_failed")
        return data


@dataclass(frozen=True)
class GitHubDescriptor:
    source_version_id: str
    commit: str
    repository_id: int
    source: str
    tree: str
    index_generation: int
    coverage: dict
    source_kind: str = "github_lazy"
    # 新来源不将ID伪装成snapshot_id；旧锚点字段显式为空。
    snapshot_id: None = None
    state: str = "PARTIAL"
    versions: dict = field(
        default_factory=lambda: {
            "source": "github-lazy-v1",
            "policy": "n3-files-v1",
            **parser_versions(),
        }
    )


@dataclass
class FetchContext:
    owner: str
    budget: NetworkBudget = field(default_factory=lambda: NetworkBudget(max_seconds=30))
    new_files: int = 0
    cache_hits: int = 0


@dataclass
class GitHubSource:
    store: object
    version: str
    client: GitHubClient
    context: FetchContext | None = None

    def descriptor(self):
        revision = self.store.revision(self.version)
        entries = self.store.entries(self.version)
        return GitHubDescriptor(
            self.version,
            revision["commit"],
            revision["repository_id"],
            revision["name"],
            revision["tree"],
            revision["index_generation"],
            {
                "catalog_entries": len(entries),
                "readable_candidates": sum(e["policy"] == "allowed" for e in entries),
                "cached": sum(
                    e["parse_state"] in {"cached", "parsed", "text_only"}
                    for e in entries
                ),
                "parsed": sum(e["parse_state"] == "parsed" for e in entries),
                "text_only": sum(e["parse_state"] == "text_only" for e in entries),
                "excluded": sum(
                    e["policy"] not in {"directory", "allowed"} for e in entries
                ),
            },
            versions={
                "source": "github-lazy-v2-text"
                if self.store.text_profile(self.version) is not None
                else "github-lazy-v1",
                "policy": "n3-files-v1",
                **parser_versions(),
            },
        )

    def for_run(self, context):
        return GitHubSource(self.store, self.version, self.client, context)

    def read_bytes(self, path):
        if self.context is None:
            raise GitHubError("network_read_requires_bound_run")
        context, store = self.context, self.store
        with store.fetch_lock:
            data = store.cached(self.version, path, context.owner, reuse=True)
            if data is not None:
                context.cache_hits += 1
            else:
                if context.new_files >= 12:
                    raise GitHubError("new_file_limit")
                entry = store.entry(self.version, path)
                revision = store.revision(self.version)
                store.mark_fetch(self.version, path, "fetching")
                try:
                    store.reserve(context.owner, entry["size"])
                    self.client.public_repository(
                        revision["name"], context.budget, revision["repository_id"]
                    )
                    data = self.client.get(
                        f"/repos/{revision['name']}/git/blobs/{entry['sha']}",
                        context.budget,
                        raw=True,
                    )
                    context.new_files += 1
                    verify_blob(data, entry)  # 完整性先于文本解释，不能被编码容错吞掉。
                    store.put(self.version, path, data, context.owner)
                except GitHubError as error:
                    store.mark_fetch(self.version, path, "failed", error.code)
                    raise
                finally:
                    store.release_reservation(context.owner)
            try:
                if not any(row[0] == path for row in store.indexed(self.version)):
                    store.index(self.version, path, data)
                return data
            except GitHubError as error:
                store.mark_fetch(self.version, path, "failed", error.code)
                if error.code in TEXT_ERRORS:
                    store.release_failed_file(
                        context.owner, self.version, path, error.code
                    )
                raise

    def read_text(self, path):
        return self.store.text_view(self.version, path, self.read_bytes(path)).text


def descriptor(source):
    if isinstance(source, (LocalSnapshotSource, GitHubSource)):
        return source.descriptor()
    return verify_snapshot(source)


def source_bytes(source, path):
    if isinstance(source, (LocalSnapshotSource, GitHubSource)):
        return source.read_bytes(path)
    return LocalSnapshotSource(Path(source)).read_bytes(path)


def load_token(selected_env, *, env_file=None, model_env=None):
    """显式白名单加载，不修改进程环境，也不在错误中包含变量值。"""
    if selected_env is None:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", selected_env) or (
        model_env is not None and selected_env.casefold() == model_env.casefold()
    ):
        raise GitHubError("github_credential_variable_rejected")
    if env_file is None:
        value = os.environ.get(selected_env)
    else:
        values = []
        for line in Path(env_file).read_text("utf-8-sig").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == selected_env:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values.append(value)
        if len(values) != 1:
            raise GitHubError("missing_or_duplicate_github_credential")
        value = values[0]
    if not value:
        raise GitHubError("missing_selected_github_credential")
    GitHubClient(token=value)  # 头部字节校验；不发送探测请求。
    return value


def prepare_github(
    store,
    client,
    url,
    ref="",
    *,
    budget=None,
    expected_id=None,
    tracking_ref=None,
    text_profile=None,
):
    name = repository_name(url)
    budget = budget or NetworkBudget(max_requests=10, max_seconds=45)
    meta = client.public_repository(name, budget, expected_id)
    selected = ref or meta["default_branch"]
    if not isinstance(selected, str) or not selected or len(selected) > 200:
        raise GitHubError("invalid_repository_ref")
    detail = client.get(
        f"/repos/{meta['full_name']}/commits/{quote(selected, safe='')}", budget
    )
    commit, tree = (
        detail.get("sha"),
        detail.get("commit", {}).get("tree", {}).get("sha"),
    )
    if (
        not isinstance(commit, str)
        or not OID.fullmatch(commit)
        or not isinstance(tree, str)
        or not OID.fullmatch(tree)
    ):
        raise GitHubError("commit_identity_failed")
    if OID.fullmatch(selected) and selected != commit:
        raise GitHubError("commit_identity_failed")
    entries = verify_catalog(
        client.get(f"/repos/{meta['full_name']}/git/trees/{tree}?recursive=1", budget),
        tree,
    )
    budget.check()  # 此后进入原子目录登记；过晚取消不将完成版本报告成未完成。
    version = store.register(
        meta,
        commit,
        tree,
        entries,
        tracking_ref or selected,
        text_profile=profile(text_profile),
    )
    return GitHubSource(store, version, client), budget


def offline_read(source, path):
    """已保留引用本地读取：新短占用与GC串行；返回后字节已在内存。"""
    owner = "read-" + uuid4().hex
    try:
        data = source.store.cached(source.version, path, owner, pin_state="reading")
        if data is None:
            raise GitHubError("retained_source_unavailable")
        return data
    finally:
        source.store.finish_run(owner, published=False)


def source_text(source, path):
    if isinstance(source, GitHubSource):
        return source.store.text_view(
            source.version, path, offline_read(source, path)
        ).text
    return source_bytes(source, path).decode("utf-8-sig")
