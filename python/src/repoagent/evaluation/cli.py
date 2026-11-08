"""G3独立CLI；保留原题集校验命令。"""

import argparse
import json
from pathlib import Path

from repoagent.evaluation.batch import preflight, read, run_batch, save
from repoagent.evaluation.protocol import seal_experiment
from repoagent.evaluation.review import (
    export_review_guide,
    export_reviews,
    freeze_dataset,
    review_status,
)
from repoagent.evaluation.scoring import export_scores, summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "preflight",
            "run",
            "export",
            "report",
            "review-export",
            "review-status",
            "review-guide",
            "freeze",
            "seal",
        ],
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--scores", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dataset", default="eval/v0.1")
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--billing", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "review-status":
            result = review_status(args.dataset, args.reviews)
        elif args.action == "review-guide":
            result = export_review_guide(args.dataset, args.output)
        elif args.action == "review-export":
            result = export_reviews(args.dataset, args.output)
        elif args.action == "freeze":
            result = freeze_dataset(
                args.dataset, args.reviews, args.output, args.version
            )
        elif args.action == "seal":
            result = seal_experiment(args.config, args.output)
        elif args.action == "preflight":
            _, result = preflight(read(args.config))
        elif args.action == "run":
            result = run_batch(read(args.config), args.batch)
        elif args.action == "export":
            result = export_scores(args.batch, args.output)
        else:
            result = summarize(args.batch, args.scores, args.billing)
            if args.output:
                save(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        # 不回显服务异常/配置内容，错误诊断在结构化结果与账本中查看。
        print(
            json.dumps(
                {"error": "evaluation_failed_check_configuration_or_batch_state"}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
