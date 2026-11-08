"""固定模型注册入口；只创建适配器，会话和账本由调用入口组装。"""

import os

from repoagent.models.chat_completions import ChatCompletionsAdapter
from repoagent.models.config import ModelConfig


def create_adapter(config: ModelConfig):
    """只按注册协议创建实例，不联网、不加载任意用户模块。"""
    config = ModelConfig.model_validate(config.model_dump())
    key = os.environ.get(config.key_env, "")
    if not key.strip():
        raise ValueError(f"{config.key_env} is not configured in the local environment")
    if config.protocol == "chat_completions":
        return ChatCompletionsAdapter(config, api_key=key)
    raise ValueError("Unsupported model protocol")
