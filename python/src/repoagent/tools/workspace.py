"""宿主绑定的多仓只读路由；公平合并候选，共享单任务执行额度。"""

import base64
import hashlib
import json

from repoagent.execution.contracts import Workspace, origin_fields
from repoagent.repositories.github import GitHubError
from repoagent.repositories.sources import (
    GitHubSource,
    SnapshotLease,
    descriptor,
    source_text,
)
from repoagent.repositories.tools import CursorStale, RepositoryTools, tool_for
from repoagent.tools.search import search_spec
from repoagent.tools.store import (
    MAX_RESULT_BYTES,
    ToolParameterError,
    json_size,
    read_text_block,
    safe_relative,
)

SOURCE_RESTRICTION_ERRORS = {
    "connection_or_tls_failed", "dns_timeout", "request_timeout", "rate_limited",
    "github_connect_timeout", "github_tls_timeout", "github_headers_timeout", "github_read_timeout",
    "github_connection_failed", "github_incomplete_response", "network_time_limit", "network_byte_limit",
    "network_request_limit", "new_file_limit", "credential_rejected", "access_denied",
    "cache_full_with_protected_content", "repository_disk_limit",
}


class WorkspaceTools:
    allows_routing = True

    def __init__(self, snapshots, binding: Workspace):
        self.roots = {name: source if isinstance(source, (GitHubSource, SnapshotLease)) else SnapshotLease.capture(source)
                      for name, source in snapshots.items()}
        self.manifests = {name: descriptor(path) for name, path in self.roots.items()}
        if Workspace.bind(self.manifests) != binding:
            raise ValueError("Workspace snapshot binding mismatch")
        self.binding = binding
        self.tools = {name: tool_for(path) for name, path in self.roots.items()}
        self.restrictions = {}

    def generations(self):
        return {
            name: source.descriptor().index_generation
            for name, source in self.roots.items()
            if isinstance(source, GitHubSource)
        }

    def _signature(self, method, params):
        parts = [self.binding.version, method, params]
        if self.generations():
            parts.append(self.generations())
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()

    def verify(self):
        if (
            Workspace.bind({k: descriptor(p) for k, p in self.roots.items()})
            != self.binding
        ):
            raise ValueError("Workspace changed")

    def _tool(self, repo_id):
        if repo_id not in self.tools:
            raise ToolParameterError("An allowed repo_id is required")
        return self.tools[repo_id]

    def _identity(self, repo_id):
        manifest = self.manifests[repo_id]
        manifest = descriptor(self.roots[repo_id])
        return dict(repo_id=repo_id, commit=manifest.commit, **origin_fields(manifest))

    def read_file(self, *, repo_id=None, **params):
        tool = self._tool(repo_id)
        symbol = params.pop("symbol", None)
        params.pop("verification_of", None)
        if symbol is not None:
            candidates = tool.get_symbol(symbol, path=params["path"], limit=100)["items"]
            if len(candidates) != 1:
                raise ToolParameterError("Symbol must identify one indexed declaration")
            params.update(start_line=candidates[0]["start_line"], end_line=candidates[0]["end_line"], start_column=0)
        source = self.roots[repo_id]
        if isinstance(source, GitHubSource) and (repo_id in self.restrictions or "*" in self.restrictions):
            safe_relative(params["path"])
            try:
                text = source_text(source, params["path"])
            except GitHubError as error:
                if error.code == "retained_source_unavailable":
                    raise GitHubError("source_retrieval_restricted") from None
                raise
            return read_text_block(self._identity(repo_id), text=text, **params)
        return tool.read_file(**params) | self._identity(repo_id)

    def outline_file(self, *, repo_id=None, **params):
        if isinstance(self.roots.get(repo_id), GitHubSource) and (repo_id in self.restrictions or "*" in self.restrictions):
            params["cached_only"] = True
        return self._tool(repo_id).outline_file(**params) | self._identity(repo_id)

    def capabilities(self):
        result = {}
        for name, source in self.roots.items():
            item = descriptor(source)
            remote = isinstance(source, GitHubSource)
            result[name] = {"source_kind": item.source_kind, "directory": "fixed_catalog",
                            "text_search": "cached_only" if remote else "prepared_readable_files",
                            "symbols": "cached_declarations" if remote else "indexed_declarations",
                            "coverage": item.coverage, "fetch_allowed_files": remote and name not in self.restrictions and "*" not in self.restrictions,
                            "declaration_kinds": "types/functions/methods as supported by language index"}
        return result

    def list_directory(self, *, repo_id=None, **params):
        if not isinstance(params.get("cursor", 0), int):
            raise ToolParameterError("Directory cursor must be an integer")
        return self._tool(repo_id).list_directory(**params) | self._identity(repo_id)

    def search_code(self, *, repo_id=None, **params):
        params = self._search_params(params)
        return self._search("search_code", repo_id, params)

    @staticmethod
    def _search_params(params):
        params = dict(params)
        spec = {key: params.pop(key) for key in ("queries", "kind", "path", "output_mode") if key in params}
        try:
            return params | search_spec(**spec)
        except (ValueError, TypeError) as error:
            raise ToolParameterError(str(error)) from None

    def get_symbol(self, *, repo_id=None, **params):
        params.setdefault("path", None)
        return self._search("get_symbol", repo_id, params)

    def clipped_cursor(self, call, items):
        params = dict(call.arguments)
        cursor = params.pop("cursor", 0)
        params.pop("limit", None)
        repository = params.pop("repo_id", None)
        if call.name == "search_code":
            params = self._search_params(params)
        else:
            params.setdefault("path", None)
        if repository is not None:
            offset = (
                json.loads(base64.urlsafe_b64decode(cursor))["offset"] if isinstance(cursor, str) else cursor
            )
            return self.tools[repository].cursor_for_offset(
                call.name, params, offset + len(items)
            )
        names = sorted(self.tools)
        if cursor:
            saved = json.loads(base64.urlsafe_b64decode(cursor))
        else:
            # 参数补默认值，保持与_search的签名一致。
            if call.name == "search_code":
                params.setdefault("kind", "text")
            elif call.name == "get_symbol":
                params.setdefault("path", None)
            key = self._signature(call.name, params)
            saved = dict(
                key=key,
                offsets={n: 0 for n in names},
                position=0,
                generations=self.generations(),
            )
        for item in items:
            saved["offsets"][item["repo_id"]] += 1
            saved["position"] = (names.index(item["repo_id"]) + 1) % len(names)
        return base64.urlsafe_b64encode(json.dumps(saved).encode()).decode()

    def _search(self, method, repo_id, params):
        if repo_id is not None:
            if not isinstance(self._tool(repo_id), RepositoryTools) and not isinstance(
                params.get("cursor", 0), int
            ) and method != "search_code":
                raise ToolParameterError("Scoped cursor must be an integer")
            result = getattr(self._tool(repo_id), method)(**params)
            identity = self._identity(repo_id)
            result["items"] = [row | identity for row in result["items"]]
            return result | identity
        limit, cursor = params.pop("limit", 20), params.pop("cursor", 0)
        names = sorted(self.tools)
        signature = self._signature(method, params)
        offsets, position = {n: 0 for n in names}, 0
        if cursor != 0:
            try:
                saved = json.loads(base64.urlsafe_b64decode(cursor))
                if saved.get("generations", {}) != self.generations():
                    raise CursorStale("cursor_stale")
                if saved["key"] != signature or set(saved["offsets"]) != set(names):
                    raise ValueError()
                offsets, position = saved["offsets"], saved["position"]
                if (
                    any(
                        type(n) is not int or not 0 <= n <= 2**31 - 1
                        for n in offsets.values()
                    )
                    or type(position) is not int
                    or not 0 <= position < len(names)
                ):
                    raise ValueError()
            except CursorStale:
                raise
            except (ValueError, TypeError, KeyError):
                raise ToolParameterError(
                    "Cursor belongs to another query/workspace"
                ) from None
        pages = {}
        for n in names:
            try:
                pages[n] = getattr(self.tools[n], method)(
                    **params,
                    limit=limit,
                    cursor=self.tools[n].cursor_for_offset(method, params, offsets[n]),
                )
            except GitHubError as error:
                if error.code not in SOURCE_RESTRICTION_ERRORS:
                    raise
                self.restrictions[n] = error.code
                pages[n] = {"items": [], "truncated": False,
                            "limitations": [f"source retrieval restricted: {error.code}; no matches is not proof of absence"]}
        result = {
            "workspace_version": self.binding.version,
            "items": [],
            "limitations": [],
            "truncated": False,
            "next_cursor": None,
            "source_restrictions": dict(self.restrictions),
        }
        if method == "search_code":
            # Each repository keeps its own coverage/count unit; missing sources never count as zero.
            keys = ("kind", "output_mode", "path_filter", "match_count", "file_count", "count_scope", "match_unit")
            result["search_summary"] = {n: {k: pages[n][k] for k in keys if k in pages[n]} for n in names}
        for n, page in pages.items():
            result["limitations"].extend(
                f"{n}: {text}" for text in page.get("limitations", [])
            )
        consumed = {n: 0 for n in names}
        while len(result["items"]) < limit:
            found = False
            for _ in names:
                n = names[position]
                position = (position + 1) % len(names)
                if consumed[n] >= len(pages[n]["items"]):
                    continue
                row = pages[n]["items"][consumed[n]] | self._identity(n)
                result["items"].append(row)
                if json_size(result) + 2048 > MAX_RESULT_BYTES:
                    result["items"].pop()
                    position = names.index(n)
                    break
                consumed[n] += 1
                offsets[n] += 1
                found = True
                break
            if not found:
                break
        if any(
            consumed[n] < len(pages[n]["items"]) or pages[n]["truncated"] for n in names
        ):
            if not result["items"]:
                raise ValueError("Workspace result exceeds response limit")
            result["truncated"] = True
            result["next_cursor"] = base64.urlsafe_b64encode(
                json.dumps(
                    dict(
                        key=signature,
                        offsets=offsets,
                        position=position,
                        generations=self.generations(),
                    )
                ).encode()
            ).decode()
        return result
