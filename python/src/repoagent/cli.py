"""命令行适配层：解析参数，显式选择模拟/协议模型，绑定快照和非金额请求记录。

业务执行交给execution，文件分析交给snapshots/tools；这里负责JSON输出和退出码。
真实调用不会自动读取.env，必须显式配置模型，并使用独立token用量文件。
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from repoagent.execution.agent import run_agent
from repoagent.execution.baseline import run_baseline
from repoagent.execution.contracts import (
    ModelRequest,
    Query,
    RepositoryBinding,
    Workspace,
)
from repoagent.execution.model_session import ModelSession
from repoagent.execution.options import add_execution_options, execution_options
from repoagent.execution.usage_store import UsageStore
from repoagent.models.capabilities import fetch_capabilities, save_capabilities
from repoagent.models.config import resolve_config
from repoagent.models.factory import create_adapter
from repoagent.models.mock import ModelServiceError, ScriptedModel
from repoagent.snapshots.java import MAX_FILE_BYTES, parse_java
from repoagent.snapshots.repository import build_snapshot, verify_snapshot
from repoagent.tools.store import SnapshotTools


def main(argv: list[str] | None = None) -> int:
    """解析argv并分发命令，输出JSON，返回进程退出码。

    参数argv为字符串列表；None表示读取终端输入，测试可直接传入列表。
    返回0表示正常完成，1用于Java语法错误，2用于输入/模型/任务失败。
    SUCCEEDED或answered仅表示协议层结果，不等于人工语义评分通过。
    """
    # Windows重定向时可能默认使用本地代码页；命令行JSON统一UTF-8。
    # 测试直接传argv时保留pytest捕获流，真实终端/console入口才调整编码。
    if argv is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # argv=None 时由 argparse 读取终端参数；测试可直接传入参数列表。
    parser = argparse.ArgumentParser(
        prog="repoagent",
        epilog="Live calls require --model-config FILE; --provider has been removed.",
    )
    # 显式选择模拟或服务配置，不隐含付费服务。
    subcommands = parser.add_subparsers(dest="command", required=True)
    inspect = subcommands.add_parser(
        "parse-java", help="Inspect one local Java file without executing it"
    )
    inspect.add_argument("file", type=Path)
    snapshot = subcommands.add_parser(
        "snapshot", help="Build a snapshot and SQLite index from a local Git commit"
    )
    snapshot.add_argument("--repo", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--ref", default="HEAD")
    for name in ("baseline", "agent"):
        command = subcommands.add_parser(name, help=f"Run an offline scripted {name}")
        command.add_argument("--snapshot", type=Path, required=True)
        command.add_argument("--repository", default="repository")
        command.add_argument("--question", required=True)
        command.add_argument("--log-text")
        add_execution_options(command)
        # 模型来源必须显式二选一。
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--mock-script", type=Path)
        source.add_argument("--model-config", type=Path)
        command.add_argument("--model")
        command.add_argument("--extensions-config", type=Path)
        command.add_argument("--capability-cache", type=Path)
        command.add_argument("--request-timeout", type=float, default=None)
        command.add_argument("--output", type=Path)
        command.add_argument(
            "--usage-ledger",
            type=Path,
            default=Path("work/usage-records/direct.sqlite3"),
        )
    smoke = subcommands.add_parser(
        "model-smoke",
        help="One configured JSON connectivity request, no repository code",
    )
    smoke.add_argument("--model")
    smoke.add_argument("--capability-cache", type=Path)
    smoke.add_argument("--model-config", type=Path, required=True)
    smoke.add_argument("--request-timeout", type=float, default=None)
    smoke.add_argument(
        "--usage-ledger", type=Path, default=Path("work/usage-records/direct.sqlite3")
    )
    smoke.add_argument("--thinking", default="default")
    smoke.add_argument("--max-output", type=int, default=None)
    capabilities = subcommands.add_parser(
        "model-capabilities",
        help="Explicitly refresh registered model metadata; no generation",
    )
    capabilities.add_argument("--model-config", type=Path, required=True)
    capabilities.add_argument("--output", type=Path, required=True)
    for name in ("coverage", "verify", "search", "read", "symbols", "ls"):
        command = subcommands.add_parser(name)
        command.add_argument("--snapshot", type=Path, required=True)
        if name in {"search", "symbols", "ls"}:
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--cursor", type=int, default=0)
        if name == "search":
            command.add_argument("--query", action="append", required=True)
            command.add_argument(
                "--kind", choices=["text", "path", "symbol"], default="text"
            )
        if name == "symbols":
            command.add_argument("--name", required=True)
            command.add_argument("--path")
        if name in {"ls", "read"}:
            command.add_argument("--path", required=name == "read", default="")
        if name == "read":
            command.add_argument("--start", type=int, default=1)
            command.add_argument("--end", type=int)
            command.add_argument("--column", type=int, default=0)
    args = parser.parse_args(argv)
    model = None

    def configured_session():
        """给真实模型绑定非金额请求记录。

        配置来自外层args；返回独立ModelSession，不修改适配器状态。
        缺配置或账本位于绑定快照内时抛ValueError，尚未发起模型请求。
        """
        timeout = args.request_timeout
        config = resolve_config(
            args.model_config,
            model=args.model,
            timeout=timeout,
            capability_cache=args.capability_cache,
        )
        adapter = create_adapter(config)
        if getattr(
            args, "snapshot", None
        ) and args.usage_ledger.resolve().is_relative_to(args.snapshot.resolve()):
            raise ValueError("Usage ledger cannot modify the bound snapshot")
        # 账本配置是执行前置条件；同一路径复用同批次，旧消耗不会因重新运行而清零。
        return ModelSession(adapter, UsageStore(args.usage_ledger))

    try:
        if args.command == "model-capabilities":
            import os

            config = resolve_config(args.model_config)
            key = os.environ.get(config.key_env, "")
            if not key.strip():
                raise ValueError("Selected model key is not configured")
            data = fetch_capabilities(config, key)
            save_capabilities(args.output, config, data)
            print(data.model_dump_json(indent=2))
            return 0
        # 只验证模型JSON连通性，不发送仓库代码；可能产生API账单，用量记录不估算金额。
        if args.command == "model-smoke":
            model = configured_session()
            model = ModelSession(
                model.adapter.with_native_thinking(args.thinking), model.store
            )
            prepared_call = model.prepare(
                ModelRequest(
                    stage="plan",
                    instruction="Connectivity test. Return a JSON plan with one query ping.",
                    query=Query(
                        workspace=Workspace.bind({"connectivity": RepositoryBinding(repository="connectivity", source_kind="local_snapshot", snapshot_id="connectivity-check", commit="0" * 40)}),
                        question="Return one text search query named ping.",
                    ),
                ),
                max_output=args.max_output,
            )
            outcome = model.invoke(prepared_call)
            response = outcome.response
            model.refresh_usage()
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "configuration": model.configuration,
                        "response": response.model_dump(),
                        "usage": outcome.record.model_dump(),
                        "usage_summary": model.store.summary(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command in {"baseline", "agent"}:
            if args.mock_script is not None:
                with args.mock_script.open("rb") as stream:
                    raw = stream.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("Mock script exceeds 1 MiB")
                script = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(script, list):
                    raise ValueError("Mock script must be a list of responses")
                responses = [
                    ModelServiceError("Simulated failure")
                    if item == {"error": "service_error"}
                    else item
                    for item in script
                ]
                model = ScriptedModel(responses)
            else:
                model = configured_session()
            # CLI绑定快照身份，执行器还会再验，保证直接调用Python入口同样受保护。
            manifest = verify_snapshot(args.snapshot)
            policy, limits = execution_options(args)
            query = Query(
                workspace=Workspace.bind({args.repository: manifest}),
                question=args.question,
                log_text=args.log_text,
                policy=policy,
            )
            # 两个入口共用执行层和初始准备；差别是一次回答还是允许追加工具反馈。
            runner = run_baseline if args.command == "baseline" else run_agent
            from repoagent.tools.extensions import ExtensionBundle, load_extensions
            bundle = ExtensionBundle(load_extensions(args.extensions_config))
            try:
                result = runner({args.repository: args.snapshot}, query, model, limits, execution_options={"extensions": bundle})
            finally:
                bundle.close()
            encoded = result.model_dump_json(indent=2)
            if args.output:
                if args.output.resolve().is_relative_to(args.snapshot.resolve()):
                    raise ValueError("Result output cannot modify the bound snapshot")
                args.output.write_text(encoded + "\n", encoding="utf-8")
            print(encoded)
            return 0 if result.execution_status == "SUCCEEDED" else 2
        if args.command == "snapshot":
            result = build_snapshot(args.repo, args.output, args.ref)
            print(result.model_dump_json(indent=2))
            return 0
        # 当前两个命令共享复验/摘要输出；exclude仅缩小展示，不跳过文件完整性校验。
        if args.command in {"coverage", "verify"}:
            # CLI绑定快照身份，执行器还会再验，保证直接调用Python入口同样受保护。
            manifest = verify_snapshot(args.snapshot)
            print(
                json.dumps(
                    manifest.model_dump(exclude={"files"}), ensure_ascii=False, indent=2
                )
            )
            return 0
        # 前面分支已return，此处剩四个直接只读工具入口，不经过模型循环。
        if args.command != "parse-java":
            tools = SnapshotTools(args.snapshot)
            if args.command == "search":
                data = tools.search_code(args.query, args.kind, args.limit, args.cursor)
            elif args.command == "read":
                data = tools.read_file(args.path, args.start, args.end, args.column)
            elif args.command == "symbols":
                data = tools.get_symbol(args.name, args.path, args.limit, args.cursor)
            else:
                data = tools.list_directory(args.path, args.limit, args.cursor)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if args.file.suffix.lower() != ".java":
            raise ValueError("Expected a .java file")
        # 最多读取“限制+1”字节：既能识别超限，又避免一次载入超大文件。
        # 不依赖事先 stat 的大小，防止文件在检查与读取之间变化。
        with args.file.open("rb") as source:
            content = source.read(MAX_FILE_BYTES + 1)
        # 输出统一使用正斜杠路径；这里只是路径展示，不代表已验证 commit。
        result = parse_java(content, args.file.as_posix())
    # 服务异常只输出安全code与用量摘要，不回显供应商原始响应或认证信息。
    except ModelServiceError as error:
        budget = getattr(model, "store", None)
        summary = None
        if budget is not None:
            cached = getattr(getattr(model, "usage", None), "usage_summary", {})
            cached = cached.get("batch", cached) if isinstance(cached, dict) else {}
            summary = dict(cached) if isinstance(cached, dict) else {}
            try:
                summary = budget.summary()
            except Exception:
                # 处理原错误时不可再抛账本错误；保留最后摘要并明确其未刷新。
                summary["refresh_error"] = "usage_summary_unavailable"
        print(
            json.dumps(
                {
                    "error": error.code,
                    "record": error.record.model_dump() if error.record else None,
                    "usage_summary": summary,
                }
            )
        )
        return 2
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    # 语法错误也返回结构化结果和警告，调用方不能把它视为完整解析。
    print(result.model_dump_json(indent=2))
    return 0 if result.parse_status == "parsed" else 1
