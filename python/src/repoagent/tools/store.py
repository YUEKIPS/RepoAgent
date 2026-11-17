"""快照只读工具：目录、字面搜索、声明定位和限量原文读取。

每次调用复验快照，SQLite用只读URI；分页/截断显式返回，避免悄悄丢失范围。
命中列表只是候选，只有执行层实际提供的完整源码行才能作为回答证据。
"""

import base64
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from repoagent.snapshots.repository import READABLE, safe_relative, verify_snapshot
from repoagent.tools.search import (
    declaration_hits,
    enclosing_types,
    file_hits,
    in_scope,
    path_hit,
    rank_hits,
    search_output,
    search_spec,
    source_lines,
)

MAX_RESULT_BYTES = 32 * 1024


class ToolParameterError(ValueError):
    """调用方可在预算内修正的参数错误；区别于索引/快照系统故障。"""


class ToolUnreadablePathError(ValueError):
    """路径合法但不在快照可读覆盖内；反馈模型可改查候选，不掩盖完整性故障。"""


def integer(value: int, minimum: int, maximum: int) -> int:
    """检查value确为int且位于闭区间minimum～maximum；bool也拒绝，成功返回原值。"""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Expected integer in [{minimum}, {maximum}]")
    return value


def json_size(value: dict) -> int:
    """返回value字典序列化后的UTF-8字节数，用于整个工具响应大小限制。"""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class SnapshotTools:
    """实例绑定一个快照；模型不能通过参数切换到任意目录或数据库。"""

    def __init__(self, snapshot: Path):
        """绑定snapshot绝对路径并复验完整性；连接在调用时按需创建，不持有可写索引。"""
        self._source = snapshot if hasattr(snapshot, "index_data") else None
        self.root = snapshot.root if self._source else snapshot.absolute()
        self._index_data = snapshot.index_data if self._source else None
        if self._source is None:
            verify_snapshot(self.root)

    def _context(self):
        """复验快照并返回(manifest, 公共元数据字典)，PARTIAL时持续附覆盖限制。"""
        manifest = self._source.descriptor() if self._source else verify_snapshot(self.root)
        base = {
            "snapshot_id": manifest.snapshot_id,
            "commit": manifest.commit,
            "state": manifest.state,
            "coverage": manifest.coverage,
            "limitations": (
                ["索引覆盖不完整；无匹配不能证明仓库不存在对应功能。"]
                if manifest.state == "PARTIAL"
                else []
            ),
        }
        return manifest, base

    def _connect(self):
        # mode=ro 禁止工具修改索引；URI 编码处理空格、# 等路径字符。
        """返回mode=ro的SQLite连接，调用者负责关闭；URI编码避免空格/#改变路径含义。"""
        if self._index_data is not None:
            db = sqlite3.connect(":memory:")
            db.deserialize(self._index_data)
            db.execute("PRAGMA query_only=ON")
            db.row_factory = sqlite3.Row
            return db
        uri = (
            "file:"
            + quote((self.root / "index.sqlite3").as_posix(), safe="/:")
            + "?mode=ro"
        )
        db = sqlite3.connect(uri, uri=True)
        db.row_factory = sqlite3.Row
        return db

    def _page(self, base: dict, rows: list[dict], limit: int, cursor: int) -> dict:
        """对rows按limit/cursor分页并合并base元数据，返回截断标记和续页游标。

        同时限制总JSON字节数；单项也无法放入时拒绝，避免游标不前进的死循环。
        """
        integer(limit, 1, 100)
        integer(cursor, 0, 2**31 - 1)
        result = base | {"items": [], "truncated": False, "next_cursor": None}
        stop = cursor
        for row in rows[cursor : cursor + limit]:
            result["items"].append(row)
            if json_size(result) + 128 > MAX_RESULT_BYTES:
                result["items"].pop()
                break
            stop += 1
        if stop < len(rows):
            if stop == cursor:
                raise ValueError("A result item exceeds tool response limit")
            result.update(truncated=True, next_cursor=stop)
        return result

    def list_directory(self, path: str = "", limit: int = 20, cursor: int = 0) -> dict:
        """列出path的直接子目录/文件，返回带版本和分页的字典。

        空path表示根目录；排除文件可见不代表可读，不存在目录抛可修正参数错误。
        """
        directory = safe_relative(path, allow_root=True)
        manifest, base = self._context()
        prefix = directory + "/" if directory else ""
        entries = {}
        for file in manifest.files:
            if file.reason == "unsafe_path" or not file.path.startswith(prefix):
                continue
            remainder = file.path[len(prefix) :]
            name, separator, _ = remainder.partition("/")
            entries[name] = {"name": name, "type": "directory" if separator else "file"}
            if not separator:
                entries[name]["status"] = file.status
        if directory and not entries:
            raise ToolParameterError("Directory not present in snapshot")
        return self._page(
            base | {"path": directory},
            [entries[key] for key in sorted(entries)],
            limit,
            cursor,
        )

    def cursor_for_offset(self, method, params, offset):
        integer(offset, 0, 2**31 - 1)
        if method != "search_code":
            return offset
        spec = search_spec(**params)
        if not offset or (spec["path"] is None and spec["output_mode"] == "content"):
            return offset
        _, base = self._context()
        key = hashlib.sha256(json.dumps([base["snapshot_id"], base["commit"], spec], sort_keys=True).encode()).hexdigest()
        return base64.urlsafe_b64encode(json.dumps(dict(key=key, offset=offset)).encode()).decode()

    def _search_offset(self, cursor, spec, base):
        if isinstance(cursor, str):
            try:
                saved = json.loads(base64.urlsafe_b64decode(cursor))
                key = hashlib.sha256(json.dumps([base["snapshot_id"], base["commit"], spec], sort_keys=True).encode()).hexdigest()
                if saved["key"] != key:
                    raise ValueError()
                return integer(saved["offset"], 0, 2**31 - 1)
            except (ValueError, TypeError, KeyError):
                raise ToolParameterError("Cursor belongs to another query/source") from None
        return integer(cursor, 0, 2**31 - 1)

    def search_code(self, queries, kind="text", limit=20, cursor=0, path=None, output_mode="content"):
        """Literal search within readable fixed-source files; output pages are mode-specific."""
        try:
            spec = search_spec(queries, kind, path, output_mode)
            integer(limit, 1, 100)
        except ValueError as error:
            raise ToolParameterError(str(error)) from None
        _, base = self._context()
        offset = self._search_offset(cursor, spec, base)
        hits = []
        with closing(self._connect()) as db:
            symbols = [dict(row) for row in db.execute("SELECT * FROM symbols")
                       if in_scope(row["path"], spec["path"])]
            if kind == "symbol":
                hits.extend(declaration_hits(symbols, spec["queries"]))
            else:
                columns = "path" if kind == "path" else "path,text"
                for row in db.execute(f"SELECT {columns} FROM files ORDER BY path"):
                    if not in_scope(row["path"], spec["path"]):
                        continue
                    if kind == "path":
                        hit = path_hit(row["path"], spec["queries"])
                        if hit:
                            hits.append(hit)
                    else:
                        hits.extend(file_hits(row["path"], row["text"], spec["queries"]))
        hits = rank_hits(hits, len(spec["queries"]))
        rows, metadata = search_output(hits, spec, "prepared_readable_files")
        if output_mode == "content":
            enclosing_types(rows[offset:offset + limit], symbols)
        page = self._page(base | metadata, rows, limit, offset)
        if page["next_cursor"] is not None:
            page["next_cursor"] = self.cursor_for_offset("search_code", spec, page["next_cursor"])
        return page

    def get_symbol(
        self, name: str, path: str | None = None, limit: int = 20, cursor: int = 0
    ) -> dict:
        """按name精确查找同名/重载声明，可用path过滤，返回分页候选字典。

        SQL参数绑定而非字符串拼接；不猜测哪个声明符合业务含义，无匹配也不证明功能不存在。
        """
        if not isinstance(name, str) or not name.strip() or len(name) > 512:
            raise ValueError("Expected a nonempty symbol name up to 512 characters")
        if path is not None:
            safe_relative(path)
        manifest, base = self._context()
        if path is not None and not any(item.path == path for item in manifest.files):
            raise ToolParameterError(
                "Symbol path must be an indexed file; omit path for global lookup"
            )
        with closing(self._connect()) as db:
            # 名字绑定SQL参数，特殊字符不能转化为SQL语句。
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM symbols WHERE name=? ORDER BY path,start_line,symbol_id",
                    (name,),
                )
                if path is None or row["path"] == path
            ]
        return self._page(
            base | {"name": name, "match_count": len(rows)}, rows, limit, cursor
        )

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
        start_column: int = 0,
        symbol: str | None = None,
        verification_of: str | None = None,
    ) -> dict:
        """读取path的start_line～end_line原文，返回范围、文本及continuation。

        行号从1起且包含结束行，start_column为0-based字符列；限200行/16KiB原文。
        按UTF-8边界截断避免坏编码，未读范围显式续读；不在覆盖内或范围非法时拒绝。
        """
        safe_relative(path)
        if symbol is not None:
            candidates = self.get_symbol(symbol, path=path, limit=100)["items"]
            if len(candidates) != 1:
                raise ToolParameterError("Symbol must identify one indexed declaration")
            start_line, end_line, start_column = candidates[0]["start_line"], candidates[0]["end_line"], 0
        integer(start_line, 1, 2**31 - 1)
        integer(start_column, 0, 2**31 - 1)
        if end_line is not None:
            integer(end_line, start_line, 2**31 - 1)
        manifest, base = self._context()
        if not any(
            file.path == path and file.status in READABLE for file in manifest.files
        ):
            raise ToolUnreadablePathError(
                "File is not readable within indexed coverage"
            )
        if self._source is not None:
            self._source.read_bytes(path)
        with closing(self._connect()) as db:
            row = db.execute("SELECT text FROM files WHERE path=?", (path,)).fetchone()
        if row is None:
            raise ValueError("File missing from index")
        return read_text_block(
            base, path, row["text"], start_line, end_line, start_column
        )

    def outline_file(self, path, limit=50, cursor=0):
        safe_relative(path)
        manifest, base = self._context()
        entry = next((f for f in manifest.files if f.path == path and f.status in READABLE), None)
        if entry is None:
            raise ToolUnreadablePathError("File is not readable within indexed coverage")
        with closing(self._connect()) as db:
            row = db.execute("SELECT text FROM files WHERE path=?", (path,)).fetchone()
            rows = [dict(r) for r in db.execute("SELECT * FROM symbols WHERE path=? ORDER BY start_line,end_line", (path,))]
        if row is None:
            raise ToolUnreadablePathError("File text unavailable")
        return self._page(base | {"path": path, "file_total_lines": len(source_lines(row["text"])),
                                 "declaration_coverage": "indexed" if entry.status == "parsed" else "text_only",
                                 "navigation_only": True}, rows, limit, cursor)


def read_text_block(base, path, text, start_line=1, end_line=None, start_column=0):
    safe_relative(path)
    integer(start_line, 1, 2**31 - 1)
    integer(start_column, 0, 2**31 - 1)
    if end_line is not None:
        integer(end_line, start_line, 2**31 - 1)
    lines = source_lines(text)
    if not lines and start_line == 1 and start_column == 0:
        return base | {"path": path, "start_line": 1, "start_column": 0, "end_line": 0,
                       "text": "", "file_empty": True, "file_total_lines": 0, "at_eof": True,
                       "requested_range": {"start_line": start_line, "end_line": end_line, "start_column": start_column},
                       "returned_range": None, "range_complete": True, "truncated": False,
                       "truncation_reason": None, "continuation": None, "next_file_range": None}
    if not lines or start_line > len(lines):
        raise ToolParameterError("Start line outside file")
    if start_column >= len(lines[start_line - 1]) and start_column != 0:
        raise ToolParameterError("Start column outside line")
    requested_end = min(end_line if end_line is not None else len(lines), len(lines))
    last = min(requested_end, start_line + 199)
    result = base | {
        "path": path,
        "start_line": start_line,
        "start_column": start_column,
        "end_line": start_line,
        "text": "",
        "truncated": False,
        "continuation": None,
        "file_total_lines": len(lines),
        "file_empty": False,
        "at_eof": False,
        "requested_range": {"start_line": start_line, "end_line": end_line, "start_column": start_column},
        "returned_range": None,
        "range_complete": False,
        "truncation_reason": None,
        "next_file_range": None,
    }
    # 支持文本控制字符只有tab/CR/LF，JSON转义最多放大两倍。
    # 预留元数据空间，确保整个返回不超过32KiB，原文不超过16KiB。
    # 原文和整个JSON分别限量，并预留转义及元数据空间；不能只限制行数。
    remaining = min(16 * 1024, (MAX_RESULT_BYTES - json_size(result) - 512) // 2)
    for number in range(start_line, last + 1):
        column = start_column if number == start_line else 0
        text = lines[number - 1][column:]
        raw = text.encode("utf-8")
        # 只在切割末尾舍弃不完整UTF-8编码；续读游标按实际字符推进，后续可取回。
        piece = raw[:remaining].decode("utf-8", errors="ignore")
        if not piece:
            result["continuation"] = {"start_line": number, "start_column": column}
            result["truncation_reason"] = "byte_limit"
            break
        result["text"] += piece
        result["end_line"] = number
        remaining -= len(piece.encode("utf-8"))
        if len(piece) < len(text):
            result["continuation"] = {
                "start_line": number,
                "start_column": column + len(piece),
            }
            result["truncation_reason"] = "byte_limit"
            break
    else:
        if last < requested_end:
            result["continuation"] = {"start_line": last + 1, "start_column": 0}
            result["truncation_reason"] = "line_limit"
    result["truncated"] = result["continuation"] is not None
    result["range_complete"] = not result["truncated"]
    partial = result["continuation"] and result["continuation"]["start_column"] > 0
    result["at_eof"] = result["end_line"] == len(lines) and not partial and not result["truncated"]
    result["returned_range"] = {"start_line": start_line, "end_line": result["end_line"], "start_column": start_column}
    if not result["at_eof"]:
        result["next_file_range"] = result["continuation"] or {"start_line": result["end_line"] + 1, "start_column": 0}
    if json_size(result) > MAX_RESULT_BYTES:
        raise ValueError("Tool response limit exceeded")
    return result
