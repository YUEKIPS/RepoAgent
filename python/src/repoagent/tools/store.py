"""快照只读工具：目录、字面搜索、声明定位和限量原文读取。

每次调用复验快照，SQLite用只读URI；分页/截断显式返回，避免悄悄丢失范围。
命中列表只是候选，只有执行层实际提供的完整源码行才能作为回答证据。
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from repoagent.snapshots.repository import READABLE, safe_relative, verify_snapshot

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


def source_lines(text: str) -> list[str]:
    """按LF分割text并保留换行，返回原文行列表。

    不使用splitlines，防止Unicode分隔符被误算成源码换行；CRLF原样保留。
    """
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def symbol_match(row: dict, term: str) -> int:
    """计算row声明对规范化term的匹配分：精确名/限定名为3，子串为1，否则0。"""
    qualified = f"{row['owner']}.{row['name']}" if row["owner"] else row["name"]
    names = [row["name"].casefold(), qualified.casefold()]
    return 3 if term in names else int(any(term in name for name in names))


class SnapshotTools:
    """实例绑定一个快照；模型不能通过参数切换到任意目录或数据库。"""

    def __init__(self, snapshot: Path):
        """绑定snapshot绝对路径并复验完整性；连接在调用时按需创建，不持有可写索引。"""
        self.root = snapshot.absolute()
        verify_snapshot(self.root)

    def _context(self):
        """复验快照并返回(manifest, 公共元数据字典)，PARTIAL时持续附覆盖限制。"""
        manifest = verify_snapshot(self.root)
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

    def search_code(
        self,
        queries: str | list[str],
        kind: str = "text",
        limit: int = 20,
        cursor: int = 0,
    ) -> dict:
        """按queries在text/path/symbol中做确定性字面检索，返回分页命中。

        支持1～3词；符号精确匹配优先，文本原大小写命中加分，多词轮流合并避免互相挤占。
        每页补所属类型位置便于读取字段/注解上下文；命中不代表完整调用图或语义检索。
        """
        if kind not in {"text", "path", "symbol"}:
            raise ValueError("Search kind must be text, path or symbol")
        queries = [queries] if isinstance(queries, str) else queries
        if not isinstance(queries, list) or not 1 <= len(queries) <= 3:
            raise ValueError("Expected 1 to 3 search queries")
        if any(
            not isinstance(q, str) or not q.strip() or len(q) > 512 for q in queries
        ):
            raise ValueError("Queries must be nonempty strings up to 512 characters")
        terms = [q.strip().casefold() for q in queries]
        integer(limit, 1, 100)
        integer(cursor, 0, 2**31 - 1)
        _, base = self._context()
        hits = []
        with closing(self._connect()) as db:
            symbols = [dict(row) for row in db.execute("SELECT * FROM symbols")]
            if kind == "symbol":
                for row in symbols:
                    score = sum(symbol_match(row, term) for term in terms)
                    if score:
                        hits.append(
                            dict(row)
                            | {
                                "score": score,
                                "_query_scores": [
                                    symbol_match(row, term) for term in terms
                                ],
                            }
                        )
            else:
                for row in db.execute("SELECT path,text FROM files ORDER BY path"):
                    if kind == "path":
                        score = sum(term in row["path"].casefold() for term in terms)
                        if score:
                            hits.append(
                                {
                                    "path": row["path"],
                                    "start_line": 1,
                                    "score": score,
                                    "_query_scores": [
                                        int(term in row["path"].casefold())
                                        for term in terms
                                    ],
                                }
                            )
                        continue
                    for number, original_line in enumerate(
                        source_lines(row["text"]), 1
                    ):
                        line = original_line.rstrip("\r\n")
                        # 文字忽略大小写可召回，保留原大小写的标识命中再加分，减少包名挤占类型声明。
                        query_scores = [
                            int(term in line.casefold())
                            + int(query.strip() in line and query.strip() != term)
                            for query, term in zip(queries, terms, strict=True)
                        ]
                        score = sum(query_scores)
                        if score:
                            hits.append(
                                {
                                    "path": row["path"],
                                    "start_line": number,
                                    "end_line": number,
                                    "snippet": line[:160],
                                    "snippet_truncated": len(line) > 160,
                                    "score": score,
                                    "_query_scores": query_scores,
                                }
                            )
        hits.sort(
            key=lambda hit: (
                -hit["score"],
                hit["path"],
                hit["start_line"],
                hit.get("name", ""),
            )
        )
        if len(terms) > 1:
            # 每个查询按自身匹配度排序，再轮流合并；宽泛词不能独占top-k。
            queues = [
                iter(
                    sorted(
                        (hit for hit in hits if hit["_query_scores"][term_index]),
                        key=lambda hit: (
                            -hit["_query_scores"][term_index],
                            hit["path"],
                            hit["start_line"],
                            hit.get("name", ""),
                        ),
                    )
                )
                for term_index in range(len(terms))
            ]
            merged, seen = [], set()
            while len(merged) < len(hits):
                for queue in queues:
                    for hit in queue:
                        identity = (
                            hit["path"],
                            hit["start_line"],
                            hit.get("end_line"),
                            hit.get("symbol_id"),
                        )
                        if identity not in seen:
                            seen.add(identity)
                            merged.append(hit)
                            break
            hits = merged
        for hit in hits:
            hit.pop("_query_scores")
        # 只为本页命中补所属类型窗口；这只是语法包含关系，不是语义类型解析。
        types = {}
        for symbol in symbols:
            if symbol["kind"] in {
                "class",
                "interface",
                "enum",
                "record",
                "annotation_type",
            }:
                types.setdefault(symbol["path"], []).append(symbol)
        for hit in hits[cursor : cursor + limit]:
            containing = [
                item
                for item in types.get(hit["path"], [])
                if item["start_line"] <= hit["start_line"]
                and hit.get("end_line", hit["start_line"]) <= item["end_line"]
            ]
            if containing:
                # 嵌套类型取包含范围最小者，便于后续读取最相关的字段/注解上下文。
                parent = min(
                    containing,
                    key=lambda item: (
                        item["end_line"] - item["start_line"],
                        item["start_line"],
                        item["symbol_id"],
                    ),
                )
                hit["enclosing_type_start_line"] = parent["start_line"]
                hit["enclosing_type_end_line"] = parent["end_line"]
        return self._page(
            base
            | {
                "kind": kind,
                "match_count": len(hits),
                "note": "无匹配不代表功能不存在。",
            },
            hits,
            limit,
            cursor,
        )

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
    ) -> dict:
        """读取path的start_line～end_line原文，返回范围、文本及continuation。

        行号从1起且包含结束行，start_column为0-based字符列；限200行/16KiB原文。
        按UTF-8边界截断避免坏编码，未读范围显式续读；不在覆盖内或范围非法时拒绝。
        """
        safe_relative(path)
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
        with closing(self._connect()) as db:
            row = db.execute("SELECT text FROM files WHERE path=?", (path,)).fetchone()
        if row is None:
            raise ValueError("File missing from index")
        return read_text_block(
            base, path, row["text"], start_line, end_line, start_column
        )


def read_text_block(base, path, text, start_line=1, end_line=None, start_column=0):
    safe_relative(path)
    integer(start_line, 1, 2**31 - 1)
    integer(start_column, 0, 2**31 - 1)
    if end_line is not None:
        integer(end_line, start_line, 2**31 - 1)
    lines = source_lines(text)
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
            break
        result["text"] += piece
        result["end_line"] = number
        remaining -= len(piece.encode("utf-8"))
        if len(piece) < len(text):
            result["continuation"] = {
                "start_line": number,
                "start_column": column + len(piece),
            }
            break
    else:
        if last < requested_end:
            result["continuation"] = {"start_line": last + 1, "start_column": 0}
    result["truncated"] = result["continuation"] is not None
    if json_size(result) > MAX_RESULT_BYTES:
        raise ValueError("Tool response limit exceeded")
    return result
