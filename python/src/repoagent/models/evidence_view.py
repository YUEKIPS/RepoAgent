"""Bind only host-emitted source slots to the immutable prepared payload."""
import hashlib
import json

from repoagent.models.base import ModelBudgetError
from repoagent.tools.store import source_lines


def numbered(item):
    return item.model_dump() | {"text": "".join(
        f"{item.start_line + i}: {line}" for i, line in enumerate(source_lines(item.text)))}


def manifest_for(prepared, request, positions=None, *, simulated=False):
    payload = json.loads(prepared.payload)
    rows = []
    for index, item in enumerate(request.evidence):
        if simulated:
            slot = {"kind": "simulated", "index": index}
            actual = payload["evidence"][index]
            expected = item.model_dump()
        elif positions is not None:
            position = positions.get(item.evidence_id)
            if position is None:
                raise ModelBudgetError(code="evidence_view_mismatch")
            slot = {"kind": "source_event", "message": position}
            container = json.loads(payload["messages"][position]["content"])
            if container.get("evidence_namespace") != request.evidence_namespace:
                raise ModelBudgetError(code="evidence_namespace_mismatch")
            actual, expected = container.get("source_event"), numbered(item)
        else:
            slot = {"kind": "inline", "message": 1, "index": index}
            actual = json.loads(payload["messages"][1]["content"])["evidence"][index]
            expected = numbered(item)
        if actual != expected:
            raise ModelBudgetError(code="evidence_view_mismatch")
        rows.append(item.model_dump(exclude={"text"}) | {
            "namespace": request.evidence_namespace,
            "text_sha256": hashlib.sha256(item.text.encode()).hexdigest(),
            "view_revision": request.evidence_view_revision,
            "wire_sha256": prepared.wire_sha256, "slot": slot})
    return rows


def verify_manifest(prepared, request, *, simulated=False):
    rows = prepared.evidence_manifest
    positions = {row["evidence_id"]: row["slot"]["message"] for row in rows
                 if row["slot"]["kind"] == "source_event"}
    expected = manifest_for(prepared, request, positions if positions else None, simulated=simulated)
    if rows != expected:
        raise ModelBudgetError(code="evidence_view_mismatch")
