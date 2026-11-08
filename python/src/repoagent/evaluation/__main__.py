"""题集校验命令入口：python -m repoagent.evaluation。

解析数据集/项目路径，调用validate_pack并输出JSON；本入口不请求模型。
"""

import argparse
import json
import sys
from pathlib import Path

from repoagent.evaluation.dataset import validate_pack


def main() -> int:
    """读取终端的dataset/project/output参数，执行离线校验并输出JSON。

    成功返回0；文件或结构错误返回2；output可选，不自动创建输出父目录。
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Offline dataset/reference validation; no model calls"
    )
    parser.add_argument("--dataset", type=Path, default=Path("eval/v0.1"))
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = validate_pack(args.dataset, args.project)
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8", newline="\n")
        print(text)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
