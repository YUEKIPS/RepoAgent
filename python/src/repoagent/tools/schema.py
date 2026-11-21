"""Versioned built-in tool parameter models and model-visible declarations."""

from typing import Literal

from pydantic import Field, model_validator

from repoagent.execution.contracts import StrictModel, ToolCall
from repoagent.execution.investigation import UpdateTaskArgs
from repoagent.snapshots.repository import safe_relative
from repoagent.tools.search import search_spec

SCHEMA_VERSION = "registered-tools-v17-scoped-search"


# 分页参数：limit限制每页项数，cursor为下一页偏移，不接受bool冒充整数。
class PageArgs(StrictModel):
    limit: int = Field(default=20, ge=1, le=100)
    cursor: int | str = 0
    repo_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
    )

    @model_validator(mode="after")
    def valid_cursor(self):
        if isinstance(self.cursor, int):
            if not 0 <= self.cursor <= 2**31 - 1:
                raise ValueError("Invalid cursor")
        elif not 1 <= len(self.cursor) <= 2048:
            raise ValueError("Invalid cursor")
        return self


# 目录请求参数，空path表示快照根目录；其余必须是安全相对路径。
class DirectoryArgs(PageArgs):
    path: str = ""

    @model_validator(mode="after")
    def safe(self):
        """校验目录路径并返回self；允许根目录，不允许越界路径。"""
        safe_relative(self.path, allow_root=True)
        return self


# 搜索参数：1～3个字面词、单一检索类型及分页信息。
class SearchArgs(PageArgs):
    queries: list[str] = Field(min_length=1, max_length=3)
    kind: Literal["text", "path", "symbol"] = "text"
    path: str | None = None
    output_mode: Literal["content", "files_with_matches", "count"] = "content"

    @model_validator(mode="after")
    def nonblank(self):
        """拒绝空白或过长的查询词，成功返回self；不将字符串作为正则执行。"""
        spec = search_spec(self.queries, self.kind, self.path, self.output_mode)
        self.queries, self.path = spec["queries"], spec["path"]
        return self


# 按声明名精确查询，可选path过滤同名候选；不自动进行语义消歧。
class SymbolArgs(PageArgs):
    name: str = Field(min_length=1, max_length=512)
    path: str | None = None

    @model_validator(mode="after")
    def safe(self):
        """检查非空声明名和可选安全路径，返回self或抛ValueError。"""
        if not self.name.strip():
            raise ValueError("Empty symbol")
        if self.path is not None:
            safe_relative(self.path)
        return self


# 原文读取参数：1-based闭区间行号，0-based字符列用于续读，不是字节偏移。
class ReadArgs(StrictModel):
    repo_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
    )
    path: str
    start_line: int = Field(default=1, ge=1, le=2**31 - 1)
    end_line: int | None = Field(default=None, ge=1, le=2**31 - 1)
    start_column: int = Field(default=0, ge=0, le=2**31 - 1)
    symbol: str | None = Field(default=None, min_length=1, max_length=512)
    verification_of: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def safe(self):
        """检查安全相对路径和行号顺序；实际文件/范围由底层工具继续检查。"""
        safe_relative(self.path)
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("Reversed range")
        if self.symbol is not None and (self.start_line != 1 or self.end_line is not None or self.start_column):
            raise ValueError("Choose a symbol or an explicit range")
        return self


class OutlineArgs(PageArgs):
    path: str

    @model_validator(mode="after")
    def safe(self):
        safe_relative(self.path)
        return self


class ExpandArgs(StrictModel):
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    catalog_cursor: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def operation(self):
        if (bool(self.evidence_ids)) == (self.catalog_cursor is not None):
            raise ValueError("Choose IDs to expand or a catalog cursor, exclusively")
        return self


ARGUMENTS = {
    "list_directory": DirectoryArgs,
    "search_code": SearchArgs,
    "read_file": ReadArgs,
    "get_symbol": SymbolArgs,
    "expand_evidence": ExpandArgs,
    "outline_file": OutlineArgs,
    "update_task": UpdateTaskArgs,
}
DESCRIPTIONS = {
    "update_task": "Optional task notebook for multi-part work. Upsert objective IDs/statuses and one canonical fact per fact_id, including conditions and registered evidence_ids. Inference/background must be labeled. Notes are NOT source evidence or proof. Update when it helps track coverage or resolve a specific uncertainty; simple answers need no notebook call. Goal/repository permissions cannot change.",
    "outline_file": "File declaration outline and total lines, navigation only. Shows indexed types/functions/methods and their ranges; text_only is not an exhaustive outline. Use read_file for source. Paginate next_cursor.",
    "expand_evidence": "Request original evidence_ids from this task for the next model input. Already visible IDs are idempotent; cold IDs are restored locally. Or supply only catalog_cursor to page the non-citable catalog. Only IDs in the actual next input's citation_policy are citable. On context_capacity request fewer IDs, or use read_file with a smaller whole-line range to create a new immutable fragment. Does not fetch another repository or version.",
    "list_directory": "列当前快照目录；排除文件可见不代表可读。按next_cursor分页。",
    "search_code": "Literal search: text=source text (case-preserving matches preferred), path=file path, symbol=indexed type/method identifier (not fields). At most 3 queries per call, trimmed and merged round-robin by their own relevance. Optional path restricts to an exact file or directory subtree; omit for global search. output_mode: content (default) returns navigation snippets; files_with_matches returns unique paths; count returns matching-line/file/declaration counts per file, not occurrences. Counts cover only count_scope, never imply whole-repository absence. Use text for field names/annotations. Follow next_cursor with the same query, kind, path and mode. Source identities may be grouped in sources by repo_id; preserve hit repo_id for subsequent reads. Search results are navigation, not citable source.",
    "read_file": "只读原文，最多200行/16KiB。省略end_line从起点读至容量边界；或指定symbol读取该文件唯一声明。range_complete仅表示请求范围完成，at_eof仅表示到了文件尾，不等于已读整个文件或必要结构。关注总行数和截断原因；按continuation续本次范围，next_file_range提示文件后续；半行不可引。verification_of可关联本次待核事项。",
    "get_symbol": "Exact bare symbol name (e.g. a type name, no 'class ' prefix). Omit path for global lookup; optional path is an EXACT FILE, never a directory. Returns candidate locations, not source text or a call graph.",
}


def tool_schemas(workspace=False) -> list[dict]:
    """从实际参数能力生成路由说明；任务级工具不承载仓库选择参数。"""
    schemas = [
        {
            "name": name,
            "description": DESCRIPTIONS[name],
            "parameters": cls.model_json_schema(),
        }
        for name, cls in ARGUMENTS.items()
    ]
    for schema in schemas:
        params = schema["parameters"]
        if workspace and "repo_id" in params["properties"]:
            schema["description"] += (
                " Workspace-bound: repo_id selects an allowed repository; omit it only for joint search/get_symbol. Copy repo_id from hits into read_file."
                " For github_lazy sources: directories and path search use the complete fixed catalog; text/symbol/get_symbol use fetched indexed files only. read_file fetches an allowed missing file within the shared budget. A miss is not proof of absence. On cursor_stale restart at cursor=0."
            )
            if schema["name"] in {"read_file", "list_directory", "outline_file"}:
                params.setdefault("required", []).append("repo_id")
        elif not workspace:
            params["properties"].pop("repo_id", None)
    return schemas


def arguments(call: ToolCall) -> dict:
    """严格校验ToolCall参数并补默认值，返回规范化字典。

    未知工具抛KeyError，非法参数抛校验错误；规范化结果也用于重复调用检测。
    """
    if call.name not in ARGUMENTS:
        raise KeyError("Unknown tool")
    params = ARGUMENTS[call.name].model_validate(call.arguments).model_dump()
    if params.get("repo_id") is None:
        params.pop("repo_id", None)
    return params
