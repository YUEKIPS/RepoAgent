"""Host-owned tool registration and contextual projection, independent of transport."""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass

from jsonschema import Draft202012Validator, ValidationError

from repoagent.tools.projection import (
    PRUNE_MARKER as PRUNE_MARKER,
)
from repoagent.tools.projection import (
    compact_navigation as compact_navigation,
)
from repoagent.tools.projection import (
    external_preview as external_preview,
)
from repoagent.tools.projection import (
    head_tail as head_tail,
)
from repoagent.tools.projection import (
    navigation_preview as navigation_preview,
)
from repoagent.tools.projection import (
    preserve as preserve,
)


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
        return self._execute_validated(call.name, self.arguments(call))

    def _execute_validated(self, name, arguments):
        """Use the same validated arguments inspected by the dispatcher."""
        spec = self._specs[name]
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
