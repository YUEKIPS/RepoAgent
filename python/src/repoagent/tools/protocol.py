"""四工具的版本化协议：严格参数模型、JSON schema、固定白名单和错误分类。

模型只能请求显式允许的函数；可修正参数错误回到循环，完整性/存储故障立即停止。
"""

import sqlite3
from typing import Literal

from jsonschema import ValidationError as SchemaError
from pydantic import Field, ValidationError, model_validator

from repoagent.execution.contracts import StrictModel, ToolCall, ToolError, ToolOutcome
from repoagent.repositories.github import GitHubError
from repoagent.repositories.text import TEXT_ERRORS
from repoagent.repositories.tools import CursorStale
from repoagent.snapshots.repository import safe_relative
from repoagent.tools.registry import (
    ToolRegistry,
    ToolSpec,
    navigation_preview,
    preserve,
)
from repoagent.tools.store import (
    SnapshotTools,
    ToolParameterError,
    ToolUnreadablePathError,
)

SCHEMA_VERSION = "registered-tools-v13-context"


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

    @model_validator(mode="after")
    def nonblank(self):
        """拒绝空白或过长的查询词，成功返回self；不将字符串作为正则执行。"""
        if any(not q.strip() or len(q) > 512 for q in self.queries):
            raise ValueError("Invalid search query")
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

    @model_validator(mode="after")
    def safe(self):
        """检查安全相对路径和行号顺序；实际文件/范围由底层工具继续检查。"""
        safe_relative(self.path)
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("Reversed range")
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
}
DESCRIPTIONS = {
    "expand_evidence": "Request original evidence_ids from this task for the next model input. Already visible IDs are idempotent; cold IDs are restored locally. Or supply only catalog_cursor to page the non-citable catalog. Only IDs in the actual next input's citation_policy are citable. On context_capacity request fewer IDs, or use read_file with a smaller whole-line range to create a new immutable fragment. Does not fetch another repository or version.",
    "list_directory": "列当前快照目录；排除文件可见不代表可读。按next_cursor分页。",
    "search_code": "Literal search: text=source text (case-preserving matches preferred), path=file path, symbol=indexed type/method identifier (not fields). Multiple queries are merged round-robin by their own relevance. Use text for field names/annotations. No matches do not prove absence; paginate with next_cursor.",
    "read_file": "只读原文，最多200行/16KiB。按continuation行列续读；半行不提供引用。",
    "get_symbol": "Exact bare symbol name (e.g. a type name, no 'class ' prefix). Omit path for global lookup; optional path is an EXACT FILE, never a directory. Returns candidate locations, not source text or a call graph.",
}


def tool_schemas(workspace=False) -> list[dict]:
    """返回四个工具的名称、描述及Pydantic参数schema；版本由SCHEMA_VERSION记录。"""
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
        if workspace:
            schema["description"] += (
                " Workspace-bound: repo_id selects an allowed repository; omit it only for joint search/get_symbol. Copy repo_id from hits into read_file."
                " For github_lazy sources: directories and path search use the complete fixed catalog; text/symbol/get_symbol use fetched indexed files only. read_file fetches an allowed missing file within the shared budget. A miss is not proof of absence. On cursor_stale restart at cursor=0."
            )
            if schema["name"] in {"read_file", "list_directory"}:
                params.setdefault("required", []).append("repo_id")
        else:
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


class EvidenceParameterError(ToolParameterError):
    def __init__(self, code):
        self.code = code if code in {"unknown_evidence_id", "context_capacity", "invalid_evidence_cursor"} else "invalid_parameters"
        super().__init__(self.code)


def builtin_registry(tools, evidence=None):
    registry = ToolRegistry()
    def source_read(result):
        data = result.copy()
        data["evidence_ids"] = evidence.add(result)
        data.pop("text")
        return data
    handlers = {"list_directory": tools.list_directory, "search_code": tools.search_code,
                "read_file": tools.read_file, "get_symbol": tools.get_symbol}
    if evidence:
        def expand(**params):
            try:
                return evidence.expand(**params)
            except ValueError as error:
                raise EvidenceParameterError(str(error)) from None
        handlers["expand_evidence"] = expand
    for schema in tool_schemas(workspace=getattr(tools, "allows_routing", False)):
        name = schema["name"]
        if name in handlers:
            registry.register(ToolSpec(name, schema["description"], schema["parameters"],
                                      handlers[name], validator=ARGUMENTS[name],
                                      normalizer=source_read if name == "read_file" and evidence else None,
                                      projector=navigation_preview if name in {"search_code", "get_symbol", "list_directory"} else preserve))
    return registry


class Dispatcher:
    """将已校验工具请求分发到固定函数，不对模型输入使用任意属性访问。"""

    def __init__(self, tools: SnapshotTools, registry=None):
        """绑定tools实例并建立四工具函数映射，模型不能切换快照或执行任意函数。"""
        self.tools = tools
        self.registry = registry or builtin_registry(tools).seal()

    def execute(self, call: ToolCall) -> ToolOutcome:
        """执行call并返回带同一调用身份的ToolOutcome。

        未知工具/参数/不可读路径可反馈修正；完整性或存储故障不可恢复，不泄露原始错误。
        """

        def failed(code: str, recoverable: bool) -> ToolOutcome:
            """按code和recoverable构造失败结果，保留外层call身份，不回显底层异常。"""
            return ToolOutcome(
                call_id=call.call_id or "missing",
                name=call.name,
                ok=False,
                error=ToolError(code=code, recoverable=recoverable),
            )

        try:
            params = self.registry.arguments(call)
        except KeyError:
            return failed("unknown_tool", True)
        except (ValueError, ValidationError, SchemaError):
            return failed("invalid_parameters", True)
        try:
            if (
                not getattr(self.tools, "allows_routing", False)
                and isinstance(self.tools, SnapshotTools)
                and isinstance(params.get("cursor", 0), str)
            ):
                raise ToolParameterError("Single snapshot cursor must be an integer")
            if self.registry.spec(call.name).origin == "builtin" and "repo_id" in params and not getattr(self.tools, "allows_routing", False):
                raise ToolParameterError("Single snapshot cannot be routed")
            # 各公开工具入口自行完整复验；此处不再对同一操作重复扫描。
            result = self.registry.execute(call)
            return ToolOutcome(
                call_id=call.call_id or "missing",
                name=call.name,
                ok=True,
                result=result,
            )
        except CursorStale:
            return failed("cursor_stale", True)
        except GitHubError as error:
            recoverable = error.code in TEXT_ERRORS or error.code in {
                "rate_limited",
                "access_denied",
                "connection_or_tls_failed",
                "github_connect_timeout", "github_tls_timeout", "github_headers_timeout",
                "github_read_timeout", "github_connection_failed", "github_incomplete_response",
                "request_timeout",
                "network_time_limit",
                "network_byte_limit",
                "network_request_limit",
                "new_file_limit",
                "file_outside_readable_catalog",
                "cache_full_with_protected_content",
                "resource_not_visible",
                "credential_rejected",
                "cancelled",
                "repository_disk_limit",
                "unsupported_text_encoding",
            }
            return failed(error.code, recoverable)
        except ToolUnreadablePathError:
            return failed("unreadable_path", True)
        except EvidenceParameterError as error:
            return failed(error.code, True)
        except ToolParameterError:
            return failed("invalid_parameters", True)
        except SchemaError:
            return failed("invalid_tool_output", True)
        except Exception as error:
            from repoagent.tools.extensions import ExtensionError
            if isinstance(error, ExtensionError):
                return failed(str(error), True)
            if isinstance(error, (OSError, ValueError, sqlite3.Error)):
                return failed("tool_failure", False)
            raise
