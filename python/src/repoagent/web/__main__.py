"""python -m repoagent.web：显式注册本地快照与模拟/真实配置。"""

import argparse
import json
import os
from pathlib import Path

from repoagent.models.config import ModelConfigSnapshot
from repoagent.repositories.github import GitHubClient
from repoagent.repositories.sources import load_token
from repoagent.web.lease import DirectoryLease
from repoagent.web.server import create_server
from repoagent.web.service import ChatService


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--output-dir", type=Path, default=Path("work/web"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mock-script", type=Path)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--extensions-config", type=Path)
    parser.add_argument("--capability-cache", type=Path)
    parser.add_argument("--initial-model-source", choices=["live", "mock"])
    parser.add_argument("--usage-ledger", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--github-token-env", metavar="VARIABLE_NAME")
    parser.add_argument("--github-env-file", type=Path)
    args = parser.parse_args(argv)
    server = None
    lease = None
    try:
        if args.mock_script is None and args.model_config is None:
            raise ValueError("Provide live configuration or mock script")
        snapshots = {}
        for item in args.snapshot:
            name, sep, path = item.partition("=")
            if not sep or not name.strip() or len(name) > 80 or name in snapshots:
                raise ValueError("Invalid or duplicate snapshot alias")
            snapshots[name] = Path(path)
        if not 1 <= args.port <= 65535:
            raise ValueError("Invalid port")
        snapshot, responses, keys, key_env = None, None, (), None
        if args.mock_script:
            if args.model_config is None and (
                (args.env_file and not args.github_token_env)
            ):
                raise ValueError("Mock mode does not accept live settings")
            raw = args.mock_script.read_bytes()
            if len(raw) > 1024 * 1024:
                raise ValueError("Mock script too large")
            responses = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(responses, list):
                raise ValueError("Mock script must be a list")
        if args.model_config:
            snapshot = ModelConfigSnapshot.load(
                args.model_config, capability_cache=args.capability_cache
            )
            key_env = snapshot.create_config().key_env
            if args.env_file:
                # 只加载配置指定的变量，不执行.env、不加载其他变量，不回显值。
                values = []
                for line in args.env_file.read_text(encoding="utf-8-sig").splitlines():
                    name, sep, value = line.strip().partition("=")
                    if sep and name.strip() == key_env:
                        value = value.strip()
                        if (
                            len(value) >= 2
                            and value[0] == value[-1]
                            and value[0] in "\"'"
                        ):
                            value = value[1:-1]
                        values.append(value)
                if len(values) != 1 or not values[0]:
                    raise ValueError("Missing or duplicate selected credential")
                os.environ[key_env] = values[0]
            keys = (os.environ.get(key_env, ""),)
        if args.github_env_file and not args.github_token_env:
            raise ValueError("GitHub env file requires explicit selected variable")
        github_token = load_token(
            args.github_token_env,
            env_file=args.github_env_file
            or (args.env_file if args.github_token_env else None),
            model_env=key_env,
        )
        if github_token and github_token in keys:
            raise ValueError("GitHub credential must be separate from model credential")
        if github_token:
            keys += (github_token,)
        # 构造服务会恢复在途状态，必须先确保没有另一服务使用同一数据库。
        output = args.output_dir.resolve()
        if any(
            output.is_relative_to(p.resolve()) or p.resolve().is_relative_to(output)
            for p in snapshots.values()
        ):
            raise ValueError("Output must be separate from snapshots")
        lease = DirectoryLease(output)
        service = ChatService(
            snapshots,
            args.output_dir,
            model_snapshot=snapshot,
            usage_path=args.usage_ledger,
            mock_responses=responses,
            secrets=keys,
            initial_model_source=args.initial_model_source,
            github_client=GitHubClient(token=github_token),
            extensions_config=args.extensions_config,
        )
        server = create_server(service, args.port)
        print(
            f"RepoAgent 本地验收：http://127.0.0.1:{args.port}（Ctrl+C退出）",
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(
            "启动失败：请核对快照、端口、模型v3/密钥与用量记录配置；未输出敏感错误原文。"
        )
        return 2
    finally:
        if server:
            server.server_close()
        if lease:
            lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
