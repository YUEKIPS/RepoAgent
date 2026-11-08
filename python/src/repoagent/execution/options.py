"""命令行共用的输出/策略选项；验证仍由运行时契约负责。"""

from repoagent.execution.contracts import ExecutionPolicy
from repoagent.execution.limits import Limits


def add_execution_options(parser):
    parser.add_argument(
        "--answer-style", choices=["auto", "brief", "detailed"], default="auto"
    )
    parser.add_argument(
        "--prompt-profile", choices=["structured"], default="structured"
    )
    parser.add_argument(
        "--retrieval-strategy", choices=["standard"], default="standard"
    )
    parser.add_argument(
        "--max-output",
        type=int,
        default=None,
        help="Full response tokens for live; JSON bytes for mock",
    )
    parser.add_argument(
        "--plan-output", type=int, help="Optional smaller planning response limit"
    )
    parser.add_argument(
        "--thinking",
        default="default",
        help="Native model option: default/off or a supported effort",
    )


def execution_options(args):
    return (
        ExecutionPolicy(
            answer_style=args.answer_style,
            prompt_profile=args.prompt_profile,
            retrieval_strategy=args.retrieval_strategy,
            native_thinking=args.thinking,
        ),
        Limits(
            max_output_estimate=args.max_output, plan_output_estimate=args.plan_output
        ),
    )
