"""模型能力、原生选择及元数据读取；能力声明不代表真实质量已经验收。"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx2
from pydantic import Field, model_validator

from repoagent.execution.contracts import StrictModel
from repoagent.models.base import ModelBudgetError, ModelServiceError


class ModelCapabilities(StrictModel):
    """受控能力快照：服务元数据不完整时，由核对过的模型配置补充。"""

    output_limit_optional: bool = True
    max_output_tokens: int | None = Field(default=None, ge=1)
    recommended_output_tokens: int | None = Field(default=None, ge=1)
    context_window: int | None = Field(default=None, ge=1)
    thinking_format: Literal["none", "deepseek", "effort"] = "none"
    can_disable_thinking: bool = False
    reasoning_levels: list[str] = Field(default_factory=list, max_length=16)
    default_reasoning_level: str | None = None
    source: str = Field(default="protocol_default_only", max_length=1000)
    fetched_at: str | None = None
    configured_fields: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def coherent(self):
        if self.configured_fields is None:
            self.configured_fields = sorted(
                self.model_fields_set - {"configured_fields", "source", "fetched_at"}
            )
        if len(set(self.reasoning_levels)) != len(self.reasoning_levels) or any(
            not x
            or len(x) > 32
            or not x.replace("_", "").replace("-", "").isalnum()
            or x in {"default", "off"}
            for x in self.reasoning_levels
        ):
            raise ValueError("Invalid native reasoning levels")
        if (
            self.default_reasoning_level is not None
            and self.default_reasoning_level not in self.reasoning_levels
        ):
            raise ValueError("Default reasoning level is unsupported")
        if (
            self.max_output_tokens is not None
            and self.recommended_output_tokens is not None
            and self.recommended_output_tokens > self.max_output_tokens
        ):
            raise ValueError("Recommended output exceeds model capability")
        return self

    def selections(self):
        return (
            ["default"]
            + (["off"] if self.can_disable_thinking else [])
            + self.reasoning_levels
        )

    def output_for(self, requested):
        """None保留真正的服务默认；不填统一1500或将自动解释为无限。"""
        if requested is None:
            if self.output_limit_optional:
                return None
            requested = self.recommended_output_tokens
            if requested is None:
                raise ModelServiceError(code="model_output_default_required")
        if type(requested) is not int or requested < 1:
            raise ModelBudgetError(code="output_tokens")
        if self.max_output_tokens is None:
            raise ModelServiceError(code="model_output_capability_required")
        if requested > self.max_output_tokens:
            raise ModelBudgetError(code="output_tokens")
        return requested

    def thinking_body(self, selected):
        if selected not in self.selections():
            raise ModelServiceError(code="unsupported_reasoning_option")
        if selected == "default":
            return {}
        if self.thinking_format == "deepseek":
            return (
                {"thinking": {"type": "disabled"}}
                if selected == "off"
                else {"thinking": {"type": "enabled"}, "reasoning_effort": selected}
            )
        if self.thinking_format == "effort" and selected != "off":
            return {"reasoning_effort": selected}
        raise ModelServiceError(code="unsupported_reasoning_option")


def fetch_capabilities(config, api_key, *, transport=None):
    """显式刷新已注册端点的/models，不跟随重定向，不回显认证或服务错误原文。"""
    try:
        with httpx2.Client(
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = client.get(
                config.base_url + "/models",
                headers={"Authorization": "Bearer " + api_key},
            )
            response.raise_for_status()
            if len(response.content) > 512_000:
                raise ValueError("Metadata too large")
            rows = response.json().get("data", [])
            if not isinstance(rows, list) or len(rows) > 200:
                raise ValueError("Invalid model list")
            matches = [
                r for r in rows if isinstance(r, dict) and r.get("id") == config.model
            ]
            if len(matches) != 1:
                raise ValueError("Model metadata missing or duplicated")
            row = matches[0]
            changes = {
                key: row[key]
                for key in ("max_output_tokens", "context_window")
                if key in row
            }
            effort = row.get("effort")
            if isinstance(effort, dict):
                if "supported_levels" in effort:
                    changes["reasoning_levels"] = effort["supported_levels"]
                if "default_level" in effort:
                    changes["default_reasoning_level"] = effort["default_level"]
            discovered = config.capabilities.model_copy(update=changes).model_dump()
            # 显式核对配置优先于元数据；缺省字段才从服务补充。
            for key in set(config.capabilities.configured_fields or []) - {
                "source",
                "fetched_at",
                "configured_fields",
            }:
                if key not in ModelCapabilities.model_fields:
                    raise ValueError("Unknown capability override")
                if getattr(config.capabilities, key) is not None:
                    discovered[key] = getattr(config.capabilities, key)
            discovered.update(
                source=config.base_url + "/models",
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
            return ModelCapabilities.model_validate(discovered)
    except Exception:
        raise ModelServiceError(code="model_capabilities_unavailable") from None


def save_capabilities(path: Path, config, capabilities):
    """缓存身份及来源，不保存密钥；调用方显式选择刷新，不在回答中自动联网。"""
    from repoagent.models.base import configuration_id

    identity = {
        k: getattr(config, k)
        for k in ("provider", "base_url", "account_scope", "model")
    }
    data = {
        "identity": identity,
        "identity_sha256": configuration_id(identity),
        "capabilities": capabilities.model_dump(),
        "configuration_sha256": configuration_id(config.model_dump()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_capabilities(path: Path, config):
    """显式缓存只适用于同一模型和原配置，不默默回落到其他身份。"""
    from repoagent.models.base import configuration_id

    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("Capability cache exceeds size limit")
    data = json.loads(raw)
    identity = {
        k: getattr(config, k)
        for k in ("provider", "base_url", "account_scope", "model")
    }
    if (
        data.get("identity") != identity
        or data.get("identity_sha256") != configuration_id(identity)
        or data.get("configuration_sha256") != configuration_id(config.model_dump())
    ):
        raise ValueError(
            "Capability cache identity/configuration mismatch; refresh explicitly"
        )
    return ModelCapabilities.model_validate(data["capabilities"])
