"""单次回答基线入口：与Agent共用准备、限制和引用校验。

基线完成初始检索后只请求最终回答，不允许模型追加工具。
"""

import time
from collections.abc import Callable
from pathlib import Path

from repoagent.execution.contracts import Query, Result
from repoagent.execution.limits import Limits
from repoagent.execution.model_session import ModelSession
from repoagent.execution.runtime import (
    Execution,
    PreparedContext,
)
from repoagent.models.base import ProviderAdapter


def run_baseline(
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
    """运行单次回答基线，返回与Agent兼容的Result。

    参数与run_agent一致；prepared用于配对复用初始规划/证据。
    区别仅为baseline模式：准备完成后一次作答，收到追加工具请求会被拒绝。
    """
    options = dict(execution_options or {})
    if saved:
        options["task_id"] = saved["task_id"]
    execution = Execution(
        snapshot, query, model, limits, cancelled, clock, "baseline", on_progress,
        **options,
    )
    if saved:
        execution.restore(saved)
    return execution.run(prepared)
