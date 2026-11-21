"""单Agent入口：复用共享执行层，以工具请求→观测→再次决策的方式工作。

支持脚本模拟及已接入的协议模型；来源、权限、时限和费用仍由宿主程序控制。
"""

import time
from collections.abc import Callable
from pathlib import Path

from repoagent.execution.contracts import Query, Result
from repoagent.execution.limits import Limits
from repoagent.execution.model_session import ModelSession
from repoagent.execution.preparation import PreparedContext
from repoagent.execution.task import run_task
from repoagent.models.base import ProviderAdapter


def run_agent(
    snapshot: Path,
    query: Query,
    model: ProviderAdapter | ModelSession,
    limits: Limits | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
    prepared: PreparedContext | None = None,
    on_progress: Callable[[dict], None] | None = None,
    execution_options=None,
    saved=None,
) -> Result:
    """运行一个受控Agent任务，返回包含终态、证据引用和轨迹的Result。

    参数snapshot/query绑定代码来源；model提供响应；limits约束调用和上下文。
    cancelled/clock可注入以测试取消和超时；prepared可复用同配置的初始准备。
    构造阶段输入或快照错误直接抛出；进入run后可预期执行错误转为失败结果。
    """
    return run_task(
        snapshot, query, model, limits, cancelled, clock, "agent", on_progress,
        prepared=prepared, execution_options=execution_options, saved=saved,
    )
