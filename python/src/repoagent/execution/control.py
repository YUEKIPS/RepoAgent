"""Shared execution stop signal and explicit byte measurement."""

import json


class StopExecution(Exception):
    """执行层内部停止信号：携带终态和安全原因，由run统一转换为Result。"""

    def __init__(self, status: str, reason: str):
        """保存status和reason供外层捕获；不把原始工具/模型异常写入结果。"""
        self.status, self.reason = status, reason


def estimate(value) -> int:
    """返回value序列化为JSON后的UTF-8字节数。

    用于上下文体积估算，不是实际tokenizer，也不表示账户费用。
    """
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
