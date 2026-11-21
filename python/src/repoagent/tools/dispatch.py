"""Bind registered tools to the active source and classify dispatch outcomes."""

import sqlite3

from jsonschema import ValidationError as SchemaError
from pydantic import ValidationError

from repoagent.execution.contracts import ToolCall, ToolError, ToolOutcome
from repoagent.execution.investigation import update_task
from repoagent.repositories.github import GitHubError
from repoagent.repositories.text import TEXT_ERRORS
from repoagent.repositories.tools import CursorStale
from repoagent.tools.projection import navigation_preview, preserve
from repoagent.tools.registry import ToolRegistry, ToolSpec
from repoagent.tools.schema import ARGUMENTS, tool_schemas
from repoagent.tools.store import (
    SnapshotTools,
    ToolParameterError,
    ToolUnreadablePathError,
)


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
    if hasattr(tools, "outline_file"):
        handlers["outline_file"] = tools.outline_file
    if evidence:
        def task_update(**params):
            try:
                return update_task(evidence.state, evidence.state.evidence, **params)
            except ValueError:
                raise ToolParameterError("Invalid task notes or evidence references") from None
        handlers["update_task"] = task_update
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
                and call.name != "search_code"
            ):
                raise ToolParameterError("Single snapshot cursor must be an integer")
            if self.registry.spec(call.name).origin == "builtin" and "repo_id" in params and not getattr(self.tools, "allows_routing", False):
                raise ToolParameterError("Single snapshot cannot be routed")
            # 各公开工具入口自行完整复验；此处不再对同一操作重复扫描。
            result = self.registry._execute_validated(call.name, params)
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
                "source_retrieval_restricted",
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
