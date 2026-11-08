"""Host-owned tool registration and contextual projection, independent of transport."""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass

from jsonschema import Draft202012Validator, ValidationError

PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"


def head_tail(text, threshold=8192, head=4096, tail=1024):
    if min(threshold, head, tail) < 0 or head + tail + len(PRUNE_MARKER) > threshold:
        raise ValueError("Invalid head/tail budget")
    if len(text) <= threshold:
        return text
    return text[:head] + PRUNE_MARKER + (text[-tail:] if tail else "")


def external_preview(result, limits):
    """Text budget spans blocks; non-text content and rich-block ordering survive."""
    result = deepcopy(result)
    blocks = result.get("content", [])
    total = sum(len(block.get("text", "")) for block in blocks if block.get("type") == "text")
    structured = result.get("structuredContent")
    # Structured JSON and its compatibility text can be identical, not additive input.
    if structured is not None:
        encoded = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        equivalents = []
        for block in blocks:
            try:
                if block.get("type") == "text" and json.loads(block["text"]) == structured:
                    equivalents.append(block)
            except (ValueError, KeyError, TypeError):
                pass
        if equivalents:
            result.pop("structuredContent")
        elif len(encoded) > limits.prune_threshold_chars:
            blocks = blocks + [{"type": "text", "text": encoded}]
            result["content"] = blocks
            result.pop("structuredContent")
        total = sum(len(block.get("text", "")) for block in blocks if block.get("type") == "text")
    if total <= limits.prune_threshold_chars:
        return result
    start, end = limits.prune_head_chars, total - limits.prune_tail_chars
    output, position, inserted = [], 0, False
    for block in blocks:
        if block.get("type") != "text":
            output.append(block)
            continue
        text = block.get("text", "")
        left = max(0, min(len(text), start - position))
        right = max(0, min(len(text), end - position))
        marker = ""
        if position < end and position + len(text) > start and not inserted:
            marker, inserted = PRUNE_MARKER, True
        visible = text[:left] + marker + text[right:]
        if visible:
            output.append({**block, "text": visible})
        position += len(text)
    result["content"] = output
    result["context_truncated"] = True
    result["original_text_chars"] = total
    return result


def navigation_preview(result, limits):
    result = deepcopy(result)
    if "items" in result:
        keys = {"path", "repo_id", "source_kind", "source_version_id", "snapshot_id", "commit", "start_line", "end_line", "name", "kind", "symbol_id"}
        result["items"] = [{k: v for k, v in item.items() if k in keys} for item in result["items"]]
        result["observation_compacted"] = True
    return result


def preserve(result, limits):
    return deepcopy(result)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: object
    origin: str = "builtin"
    validator: object = None
    projector: object = preserve
    output_schema: dict | None = None
    binding: dict | None = None
    normalizer: object = None


class ToolRegistry:
    def __init__(self):
        self._specs, self._sealed = {}, False

    def register(self, spec):
        if self._sealed or spec.name in self._specs or spec.origin not in {"builtin", "external"}:
            raise ValueError("Invalid or frozen tool registration")
        if not spec.name or len(spec.name) > 80:
            raise ValueError("Invalid tool name")
        def local_refs(value):
            if isinstance(value, dict):
                if "$ref" in value and not value["$ref"].startswith("#"):
                    raise ValueError("Remote schema references are not allowed")
                for item in value.values():
                    local_refs(item)
            elif isinstance(value, list):
                for item in value:
                    local_refs(item)
        local_refs(spec.input_schema)
        local_refs(spec.output_schema)
        Draft202012Validator.check_schema(spec.input_schema)
        if spec.output_schema:
            Draft202012Validator.check_schema(spec.output_schema)
        self._specs[spec.name] = ToolSpec(**{**vars(spec), "input_schema": deepcopy(spec.input_schema), "output_schema": deepcopy(spec.output_schema), "binding": deepcopy(spec.binding)})

    def seal(self):
        self._sealed = True
        return self

    def names(self):
        return tuple(self._specs)

    def spec(self, name):
        spec = self._specs[name]
        return ToolSpec(**{**vars(spec), "input_schema": deepcopy(spec.input_schema), "output_schema": deepcopy(spec.output_schema), "binding": deepcopy(spec.binding)})

    def schemas(self):
        return [{"name": s.name, "description": s.description, "parameters": deepcopy(s.input_schema)} for s in self._specs.values()]

    def arguments(self, call):
        spec = self._specs[call.name]
        if spec.validator:
            result = spec.validator.model_validate(call.arguments).model_dump()
            if result.get("repo_id") is None:
                result.pop("repo_id", None)
            return result
        try:
            Draft202012Validator(spec.input_schema).validate(call.arguments)
        except ValidationError:
            raise ValueError("Invalid external tool arguments") from None
        return deepcopy(call.arguments)

    def execute(self, call):
        arguments = self.arguments(call)
        spec = self._specs[call.name]
        result = spec.handler(**arguments)
        if spec.output_schema and "structuredContent" in result and not result.get("isError"):
            Draft202012Validator(spec.output_schema).validate(result["structuredContent"])
        return result

    def project(self, observation, limits):
        projected = observation.model_copy(deep=True)
        if projected.outcome.ok:
            spec = self._specs.get(projected.call.name)
            if spec:
                projected.outcome.result = external_preview(projected.outcome.result, limits) if spec.origin == "external" else spec.projector(projected.outcome.result, limits)
        return projected

    @property
    def fingerprint(self):
        rows = [{"name": s.name, "description": s.description, "schema": s.input_schema,
                 "output_schema": s.output_schema, "origin": s.origin, "binding": s.binding,
                 "projector": getattr(s.projector, "__name__", "custom"),
                 "normalizer": getattr(s.normalizer, "__name__", None)} for s in self._specs.values()]
        return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
