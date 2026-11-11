"""版本化服务配置；协议决定代码路径，服务/账户/参数只作为数据。"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, JsonValue, model_validator

from repoagent.execution.contracts import StrictModel
from repoagent.models.capabilities import ModelCapabilities

# 扩展只进入JSON体，不能改变宿主控制或覆盖标准消息/工具参数。
PROTECTED = {
    "model",
    "messages",
    "tools",
    "tool_choice",
    "response_format",
    "stream",
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "n",
    "timeout",
    "max_retries",
    "extra_headers",
    "extra_query",
    "extra_body",
    "api_key",
    "base_url",
    "authorization",
    "headers",
    "temperature",
    "service_tier",
}


class GenerationConfig(StrictModel):
    temperature: float = Field(default=0.0, ge=0, le=2, allow_inf_nan=False)
    extra_body: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_extensions(self):
        if any(key.lower() in PROTECTED for key in self.extra_body):
            raise ValueError("Generation extension overrides a controlled parameter")
        if len(json.dumps(self.extra_body, allow_nan=False).encode()) > 4096:
            raise ValueError("Generation extensions exceed size limit")

        def bounded(value, depth=0):
            if depth > 6:
                raise ValueError("Generation extensions exceed nesting limit")
            if isinstance(value, (dict, list)):
                if len(value) > 32:
                    raise ValueError("Generation extensions exceed item limit")
                for child in value.values() if isinstance(value, dict) else value:
                    bounded(child, depth + 1)

        bounded(self.extra_body)
        return self


class OutputConfig(StrictModel):
    mode: Literal["json_object"] = "json_object"
    tools: Literal["native", "none"] = "native"


class UsageMapping(StrictModel):
    input_tokens: str = "prompt_tokens"
    output_tokens: str = "completion_tokens"
    cache_hit_input_tokens: str | None = None
    cache_miss_input_tokens: str | None = None
    reasoning_tokens: str | None = "completion_tokens_details.reasoning_tokens"

    @model_validator(mode="after")
    def simple_paths(self):
        for value in self.model_dump().values():
            if value is not None and not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,3}", value
            ):
                raise ValueError("Usage mapping requires simple JSON field paths")
        return self


class ModelConfig(StrictModel):
    schema_version: Literal[3]
    protocol: Literal["chat_completions"]
    provider: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    model: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=1000)
    key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    account_scope: str = Field(default="default", pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    request_timeout_seconds: float = Field(
        default=30.0, gt=0, le=180,
        description="Network read/write/pool inactivity timeout; connect capped at 15 seconds. Not response total time.",
    )
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    usage_mapping: UsageMapping = Field(default_factory=UsageMapping)
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)

    @model_validator(mode="after")
    def native_controls(self):
        if any(
            k.lower() in {"thinking", "reasoning_effort", "reasoning"}
            for k in self.generation.extra_body
        ):
            raise ValueError(
                "Model v3 uses native thinking selection, not raw generation overrides"
            )
        return self

    @model_validator(mode="after")
    def valid_endpoint(self):
        url = urlsplit(self.base_url)
        if (
            not self.model.strip()
            or url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or "?" in self.base_url
            or "#" in self.base_url
            or "\\" in self.base_url
            or re.search(r"\s|[\x00-\x1f\x7f]", self.base_url)
            or "%" in url.netloc
            or any(p in {".", ".."} for p in url.path.split("/"))
        ):
            raise ValueError(
                "Require an HTTPS base URL without credentials/query/fragment"
            )
        port = url.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid service port")
        host = url.hostname.lower().encode("idna").decode()
        if ":" in host:
            host = "[" + host + "]"
        if port not in (None, 443):
            host += ":" + str(port)
        self.base_url = urlunsplit(("https", host, url.path.rstrip("/"), "", ""))
        return self

    @classmethod
    def load(cls, path: Path):
        """普通调用只取校验配置；需要记录来源时使用ModelConfigSnapshot。"""
        return cls.from_bytes(path.read_bytes())

    @classmethod
    def from_bytes(cls, content: bytes):
        """从调用方已捕获的同一份字节解析，不再次访问文件。"""
        raw = json.loads(content.decode("utf-8-sig"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 3:
            raise ValueError(
                "Model config v3 required; historical configurations are unsupported"
            )
        return cls.model_validate(raw)


@dataclass(frozen=True)
class ModelConfigSnapshot:
    """固定一次文件读取的来源/原字节摘要与解析后JSON，避免批次运行时重读漂移。"""

    source_path: str
    source_sha256: str
    resolved_json: str
    resolved_sha256: str

    @classmethod
    def load(cls, path=None, *, model=None, timeout=None, capability_cache=None):
        if path is None:
            raise ValueError(
                "Live calls require --model-config; replace the removed --provider option"
            )
        path = Path(path)
        content = path.read_bytes()
        config = ModelConfig.from_bytes(content)
        if capability_cache is not None:
            from repoagent.models.capabilities import load_capabilities

            config.capabilities = load_capabilities(Path(capability_cache), config)
        if model is not None and model != config.model:
            raise ValueError("Model CLI option conflicts with model config")
        if timeout is not None and timeout != config.request_timeout_seconds:
            raise ValueError("Timeout CLI option conflicts with model config")
        resolved = json.dumps(
            config.model_dump(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            path.resolve().as_posix(),
            hashlib.sha256(content).hexdigest(),
            resolved,
            hashlib.sha256(resolved.encode("utf-8")).hexdigest(),
        )

    def create_config(self):
        """各任务得到独立配置对象，共享不可变JSON快照，不共享可变嵌套对象。"""
        return ModelConfig.model_validate_json(self.resolved_json)


def resolve_config(path=None, *, model=None, timeout=None, capability_cache=None):
    """真实入口必须显式配置；CLI不能静默覆盖文件。"""
    return ModelConfigSnapshot.load(
        path, model=model, timeout=timeout, capability_cache=capability_cache
    ).create_config()
