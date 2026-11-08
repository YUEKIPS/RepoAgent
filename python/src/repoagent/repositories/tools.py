"""按需版本的四工具：目录全覆盖，正文/符号只搜索已取得的有效索引。"""

import base64
import hashlib
import json

from repoagent.execution.contracts import origin_fields
from repoagent.repositories.github import GitHubError
from repoagent.repositories.sources import GitHubSource, LocalSnapshotSource
from repoagent.tools.store import (
    MAX_RESULT_BYTES,
    SnapshotTools,
    ToolParameterError,
    integer,
    json_size,
    read_text_block,
    safe_relative,
    source_lines,
)


class CursorStale(ToolParameterError):
    pass


def tool_for(source):
    if isinstance(source, GitHubSource):
        return RepositoryTools(source)
    return SnapshotTools(
        source.root if isinstance(source, LocalSnapshotSource) else source
    )


class RepositoryTools:
    single_source = True

    def __init__(self, source):
        self.source = source

    def _base(self):
        d = self.source.descriptor()
        return origin_fields(d) | {
            "commit": d.commit,
            "state": d.state,
            "coverage": d.coverage,
            "index_generation": d.index_generation,
            "limitations": [
                "按需覆盖：正文和符号仅检索已缓存且索引有效的文件；未命中不证明全仓不存在。"
            ],
        }

    def _page(self, rows, base, limit, cursor, key):
        integer(limit, 1, 100)
        signature = hashlib.sha256(
            json.dumps([self.source.version, key], sort_keys=True).encode()
        ).hexdigest()
        generation = self.source.descriptor().index_generation
        offset = cursor
        if cursor != 0:
            try:
                saved = json.loads(base64.urlsafe_b64decode(cursor))
                if saved["key"] != signature:
                    raise ToolParameterError("Cursor belongs to another query")
                if saved["generation"] != generation:
                    raise CursorStale("cursor_stale")
                offset = saved["offset"]
                integer(offset, 0, 2**31 - 1)
            except (CursorStale, ToolParameterError):
                raise
            except (ValueError, TypeError, KeyError):
                raise ToolParameterError("Invalid remote cursor") from None
        else:
            offset = 0
        result = base | {"items": [], "truncated": False, "next_cursor": None}
        for row in rows[offset : offset + limit]:
            result["items"].append(row)
            if json_size(result) + 512 > MAX_RESULT_BYTES:
                result["items"].pop()
                break
        end = offset + len(result["items"])
        if end < len(rows):
            if end == offset:
                raise ValueError("Result item exceeds tool limit")
            result.update(
                truncated=True,
                next_cursor=base64.urlsafe_b64encode(
                    json.dumps(
                        {"key": signature, "generation": generation, "offset": end}
                    ).encode()
                ).decode(),
            )
        return result

    def cursor_for_offset(self, method, params, offset):
        if not offset:
            return 0
        key = [method, params]
        signature = hashlib.sha256(
            json.dumps([self.source.version, key], sort_keys=True).encode()
        ).hexdigest()
        return base64.urlsafe_b64encode(
            json.dumps(
                {
                    "key": signature,
                    "generation": self.source.descriptor().index_generation,
                    "offset": offset,
                }
            ).encode()
        ).decode()

    def list_directory(self, path="", limit=20, cursor=0):
        safe_relative(path, allow_root=True)
        prefix, rows = path + "/" if path else "", {}
        for entry in self.source.store.entries(self.source.version):
            if not entry["path"].startswith(prefix):
                continue
            rest = entry["path"][len(prefix) :]
            name, separator, _ = rest.partition("/")
            if not name:
                continue
            rows[name] = {
                "name": name,
                "type": "directory" if separator or entry["type"] == "tree" else "file",
            }
            if not separator and entry["type"] != "tree":
                rows[name].update(
                    policy=entry["policy"],
                    status=entry["parse_state"],
                    fetch_state=entry["fetch_state"],
                    fetch_error=entry["fetch_error"],
                )
        if path and not rows:
            raise ToolParameterError("Directory not in fixed catalog")
        # 固定目录的页码不因索引增长改变，保留简单整型目录偏移。
        integer(cursor, 0, 2**31 - 1)
        return SnapshotTools._page(
            self,
            self._base() | {"path": path},
            [rows[k] for k in sorted(rows)],
            limit,
            cursor,
        )

    def search_code(self, queries, kind="text", limit=20, cursor=0):
        if (
            kind not in {"path", "text", "symbol"}
            or not 1 <= len(queries) <= 3
            or any(
                not isinstance(q, str) or not q.strip() or len(q) > 512 for q in queries
            )
        ):
            raise ToolParameterError("Invalid search query")
        rows, terms = [], [q.casefold() for q in queries]
        if kind == "path":
            for e in self.source.store.entries(self.source.version):
                if e["policy"] == "allowed" and any(
                    t in e["path"].casefold() for t in terms
                ):
                    rows.append(
                        {
                            "path": e["path"],
                            "start_line": 1,
                            "end_line": 1,
                            "status": e["parse_state"],
                            "score": sum(t in e["path"].casefold() for t in terms),
                        }
                    )
        else:
            for path, state, symbols in self.source.store.indexed(self.source.version):
                if kind == "symbol":
                    rows.extend(
                        s | {"score": 1}
                        for s in symbols
                        if any(t in s["name"].casefold() for t in terms)
                    )
                else:
                    data = self.source.store.cached(
                        self.source.version, path, self.source.context.owner
                    )
                    if data is None:
                        raise GitHubError("indexed_content_unavailable")
                    for number, line in enumerate(
                        source_lines(
                            self.source.store.text_view(
                                self.source.version, path, data
                            ).text
                        ),
                        1,
                    ):
                        if any(t in line.casefold() for t in terms):
                            rows.append(
                                {
                                    "path": path,
                                    "start_line": number,
                                    "end_line": number,
                                    "snippet": line[:400],
                                    "score": sum(t in line.casefold() for t in terms),
                                }
                            )
        rows.sort(key=lambda r: (-r.get("score", 0), r["path"], r["start_line"]))
        return self._page(
            rows,
            self._base(),
            limit,
            cursor,
            ["search_code", {"queries": queries, "kind": kind}],
        )

    def get_symbol(self, name, path=None, limit=20, cursor=0):
        if not name.strip() or len(name) > 512:
            raise ToolParameterError("Invalid symbol")
        if path:
            safe_relative(path)
        rows = [
            s
            for p, state, symbols in self.source.store.indexed(self.source.version)
            if path is None or path == p
            for s in symbols
            if s["name"] == name
        ]
        return self._page(
            rows,
            self._base(),
            limit,
            cursor,
            ["get_symbol", {"name": name, "path": path}],
        )

    def read_file(self, path, start_line=1, end_line=None, start_column=0):
        safe_relative(path)
        text = self.source.read_text(path)
        return read_text_block(
            self._base(),
            path,
            text,
            start_line,
            end_line,
            start_column,
        )
