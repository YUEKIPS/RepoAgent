"""Append-only conversation facts, current surface, and derived messages.

This session never sends requests or executes tools. Returned values are detached
snapshots; SQLite persistence and execution checkpoints share a transaction.
"""

import hashlib
import json
import time
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repoagent.execution.contracts import (
    ConversationSummary,
    ModelResponse,
    Observation,
    Result,
    ToolCall,
    ToolOutcome,
)

SESSION_VERSION = "conversation-events-v2-context"


class Data(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str = Field(min_length=1)
    step: int = Field(default=0, ge=0)


class UserData(Data):
    question: str
    log_text: str | None = None


class AssistantData(Data):
    response: ModelResponse


class CallData(Data):
    call: ToolCall


class OutcomeData(Data):
    observation: Observation
    raw_outcome: ToolOutcome | None = None

    @model_validator(mode="after")
    def identity(self):
        if self.raw_outcome and (self.raw_outcome.call_id, self.raw_outcome.name) != (
            self.observation.call.call_id, self.observation.call.name
        ):
            raise ValueError("Raw tool result identity mismatch")
        return self


class SystemData(Data):
    text: str


class SummaryData(Data):
    summary: ConversationSummary


class BoundaryData(Data):
    stage: str | None = None
    status: str | None = None


class RequestData(Data):
    identity: dict


class AttemptData(Data):
    error_code: str


class CommitData(Data):
    result: Result | None = None
    error: dict | None = None


class PruneData(Data):
    source_seq: int = Field(ge=0)
    before_sha256: str
    after_sha256: str
    registry_fingerprint: str
    reason: str


class StepMemoryData(Data):
    memory: dict


SCHEMAS = {
    "user/message": UserData,
    "assistant/message": AssistantData,
    "assistant/plan": AssistantData,
    "context/summary_response": AssistantData,
    "tool/call": CallData,
    "tool/result": OutcomeData,
    "host/tool_result": OutcomeData,
    "tool/settled": OutcomeData,
    "system/message": SystemData,
    "context/summary": SummaryData,
    "context/checkpoint": SummaryData,
    "run/start": BoundaryData,
    "run/end": BoundaryData,
    "step/start": BoundaryData,
    "step/end": BoundaryData,
    "request/prepared": RequestData,
    "assistant/attempt": AttemptData,
    "answer/committed": CommitData,
    "context/prune": PruneData,
    "context/step_checkpoint": StepMemoryData,
}
SURFACE_TYPES = {
    "system/message", "user/message", "assistant/message", "tool/result",
    "context/checkpoint",
    "context/step_checkpoint",
}


class Replace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    op: Literal["replace"]
    start_seq: int = Field(ge=0)
    end_seq: int = Field(ge=0)


class SessionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    seq: int = Field(ge=0)
    time: int = Field(ge=0)
    type: str
    data: dict
    surface_op: Literal["append"] | Replace | None = None
    source_event_seqs: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def typed_payload(self):
        schema = SCHEMAS.get(self.type)
        if schema is None:
            raise ValueError("Unknown session event type")
        schema.model_validate(self.data)
        expected = {"assistant/plan": {"plan"}, "context/summary_response": {"summary"},
                    "assistant/message": {"answer", "tool_call", "tool_calls"}}.get(self.type)
        if expected and self.data["response"]["kind"] not in expected:
            raise ValueError("Session assistant event kind mismatch")
        if (self.type in SURFACE_TYPES) != (self.surface_op is not None):
            raise ValueError("Invalid session surface eligibility")
        if self.type not in SURFACE_TYPES and self.source_event_seqs:
            raise ValueError("Log-only event cannot cite surface sources")
        if len(set(self.source_event_seqs)) != len(self.source_event_seqs) or any(
            type(s) is not int or not 0 <= s < self.seq for s in self.source_event_seqs
        ):
            raise ValueError("Invalid session source references")
        return self


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class ConversationSession:
    """One authoritative log; surface and messages are disposable projections."""

    def __init__(self, session_id, workspace_version, events=()):
        self.session_id, self.workspace_version = session_id, workspace_version
        self._events, self._nodes, self._digests = [], [], []
        for event in events:
            self._admit(SessionEvent.model_validate(deepcopy(event)))

    @property
    def seq(self):
        return len(self._events)

    @property
    def surface(self):
        return tuple(self._nodes)

    @property
    def revision(self):
        return hashlib.sha256(encode([self.session_id, self.workspace_version, self._digests]).encode()).hexdigest()

    def _admit(self, event):
        if event.seq != self.seq:
            raise ValueError("Non-contiguous session log")
        if event.type in {"tool/result", "host/tool_result", "tool/settled"}:
            call = event.data["observation"]["call"]
            known = [e for e in self._events if e.type == "tool/call" and
                     e.data["run_id"] == event.data["run_id"] and e.data["call"]["call_id"] == call["call_id"]]
            if not known or known[-1].data["call"] != call:
                raise ValueError("Session tool result has no matching call")
            if event.surface_op == "append" and any(
                e.type == event.type and e.data["run_id"] == event.data["run_id"] and
                e.data["observation"]["call"]["call_id"] == call["call_id"] for e in self._events
            ):
                raise ValueError("Duplicate session tool result")
        if event.type in {"context/summary", "context/checkpoint"}:
            summary = event.data["summary"]
            if (summary["session_id"], summary["workspace_version"]) != (
                self.session_id, self.workspace_version
            ):
                raise ValueError("Session summary binding mismatch")
        nodes = self._nodes.copy()
        op = event.surface_op
        if op == "append":
            nodes.append(event.seq)
        elif isinstance(op, Replace):
            if op.start_seq not in nodes or op.end_seq not in nodes:
                raise ValueError("Replacement endpoints are not current surface nodes")
            left, right = nodes.index(op.start_seq), nodes.index(op.end_seq)
            if left > right or not set(nodes[left:right + 1]) <= set(event.source_event_seqs):
                raise ValueError("Replacement does not cover its sources")
            old = self._events[nodes[left]]
            if old.type == "system/message" and (
                event.type != "system/message" or left != right
            ):
                raise ValueError("Cannot shadow system prompt with conversation data")
            if event.type == "tool/result" and (
                left != right or old.type != "tool/result" or
                event.data["observation"]["call"] != old.data["observation"]["call"]
            ):
                raise ValueError("Tool replacement must retain call identity")
            if event.type == "context/step_checkpoint":
                selected = nodes[left:right + 1]
                if any(self._events[n].type not in {"assistant/message", "tool/result", "context/step_checkpoint"} or
                       self._events[n].data["run_id"] != event.data["run_id"] for n in selected):
                    raise ValueError("Step memory must replace a same-run tool span")
                self.validate_tool_pairs([m for m in self.derive_messages() if m["event_seq"] in selected])
            nodes[left:right + 1] = [event.seq]
        # Validate/copy JSON before publishing either the event or its projection.
        raw = encode(event.model_dump())
        stored = event.model_copy(deep=True)
        self._events.append(stored)
        self._digests.append(hashlib.sha256(raw.encode()).hexdigest())
        self._nodes = nodes

    def append(self, kind, data, *, surface_op=None, source_event_seqs=()):
        event = SessionEvent.model_validate({
            "seq": self.seq, "time": time.time_ns() // 1_000_000,
            "type": kind, "data": deepcopy(data), "surface_op": surface_op,
            "source_event_seqs": list(source_event_seqs),
        })
        self._admit(event)
        return SessionEvent.model_validate(deepcopy(event.model_dump()))

    def events(self):
        return [deepcopy(e.model_dump()) for e in self._events]

    def export(self):
        return {"version": SESSION_VERSION, "session_id": self.session_id,
                "workspace_version": self.workspace_version, "events": self.events()}

    @classmethod
    def restore(cls, value):
        if set(value) != {"version", "session_id", "workspace_version", "events"} or value["version"] != SESSION_VERSION:
            raise ValueError("Unsupported conversation session")
        return cls(value["session_id"], value["workspace_version"], value["events"])

    def observations(self, run_id):
        visible = {m["event_seq"] for m in self.derive_messages(run_id=run_id, roles={"tool"})}
        return [Observation.model_validate(deepcopy(e.data["observation"]))
                for e in self._events
                if e.type in {"tool/result", "host/tool_result"} and e.data["run_id"] == run_id
                and (e.type == "host/tool_result" or e.seq in visible)]

    def has_run(self, run_id):
        return any(e.type == "run/start" and e.data["run_id"] == run_id for e in self._events)

    def has_settled_tool(self, run_id, call_id):
        return any(e.type == "tool/settled" and e.data["run_id"] == run_id and
                   e.data["observation"]["call"]["call_id"] == call_id for e in self._events)

    def current_system(self):
        return next((deepcopy(e.model_dump()) for e in reversed(self._events)
                     if e.type == "system/message" and e.seq in self._nodes), None)

    def record_observation(self, run_id, observation, *, step=0, raw_outcome=None):
        host = observation.call.call_id.startswith("initial-")
        recorded = next((e for e in self._events if e.type == "tool/call" and e.data["run_id"] == run_id and
                         e.data["call"]["call_id"] == observation.call.call_id), None)
        if recorded is None:
            self.append("tool/call", {"run_id": run_id, "step": step, "call": observation.call.model_dump()})
        else:
            step = recorded.data.get("step", 0)
        self.append("host/tool_result" if host else "tool/result", {
            "run_id": run_id, "step": step, "observation": observation.model_dump(),
            "raw_outcome": raw_outcome.model_dump() if raw_outcome else None,
        }, surface_op=None if host else "append")

    def derive_messages(self, *, run_id=None, require_complete=False, roles=None):
        messages = []
        for seq in self._nodes:
            event = self._events[seq]
            data = event.data
            if run_id is not None and data["run_id"] != run_id:
                continue
            expected_role = {"user/message": "user", "system/message": "system", "assistant/message": "assistant",
                             "tool/result": "tool", "context/checkpoint": "user", "context/step_checkpoint": "user"}[event.type]
            if roles is not None and expected_role not in roles:
                continue
            role, content = None, []
            if event.type == "user/message":
                role, content = "user", [{"type": "text", "text": data["question"]}]
                if data.get("log_text"):
                    content.append({"type": "text", "text": data["log_text"]})
            elif event.type == "system/message":
                if data["text"]:
                    role, content = "system", [{"type": "text", "text": data["text"]}]
            elif event.type == "assistant/message":
                response = ModelResponse.model_validate(data["response"])
                role = "assistant"
                if response.answer:
                    content.append({"type": "text", "text": response.answer.answer})
                for call in response.tool_calls or ([response.tool_call] if response.tool_call else []):
                    content.append({"type": "tool-call", **call.model_dump()})
            elif event.type == "tool/result":
                role, content = "tool", [{"type": "tool-result", **deepcopy(data["observation"]["outcome"])}]
            elif event.type == "context/checkpoint":
                role, content = "user", [{"type": "text", "text": encode(data["summary"]["result"])}]
            elif event.type == "context/step_checkpoint":
                role, content = "user", [{"type": "text", "text": encode(data["memory"])}]
            if role and content:
                messages.append({"event_seq": seq, "run_id": data["run_id"], "role": role, "content": content})
        if require_complete:
            self.validate_tool_pairs(messages)
        return messages

    @staticmethod
    def validate_tool_pairs(messages):
        pending, seen = set(), set()
        for message in messages:
            for block in message["content"]:
                key = (message["run_id"], block.get("call_id"))
                if block["type"] == "tool-call":
                    if key in seen:
                        raise ValueError("Duplicate derived tool call")
                    pending.add(key)
                    seen.add(key)
                elif block["type"] == "tool-result":
                    if key not in pending:
                        raise ValueError("Unpaired derived tool result")
                    pending.remove(key)
        if pending:
            raise ValueError("Unfinished derived tool calls")

    def latest_summary(self):
        for event in reversed(self._events):
            if event.type == "context/summary":
                return ConversationSummary.model_validate(deepcopy(event.data["summary"]))
        return None

    def apply_summary(self, summary, run_id):
        sources = set(summary.source_turn_ids)
        known = {e.data["run_id"] for e in self._events if e.type == "user/message"}
        if not sources <= known or run_id in sources:
            raise ValueError("Summary source events are missing or still active")
        selected = [seq for seq in self._nodes if (
            self._events[seq].type != "system/message" and (
                self._events[seq].data["run_id"] in sources or
                (self._events[seq].type == "context/checkpoint" and
                 set(self._events[seq].data["summary"]["source_turn_ids"]) <= sources)
            )
        )]
        if not selected:
            raise ValueError("Summary has no current source span")
        left, right = self._nodes.index(selected[0]), self._nodes.index(selected[-1])
        if self._nodes[left:right + 1] != selected:
            raise ValueError("Summary would shadow unrelated current messages")
        self.validate_tool_pairs([m for m in self.derive_messages() if m["event_seq"] in selected])
        record = self.append("context/summary", {"run_id": run_id, "summary": summary.model_dump()})
        self.append("context/checkpoint", {"run_id": run_id, "summary": summary.model_dump()},
                    surface_op={"op": "replace", "start_seq": selected[0], "end_seq": selected[-1]},
                    source_event_seqs=[record.seq, *selected])

    def turn_jobs(self):
        """Existing turn policy consumes this projection, never mutable UI jobs."""
        rows = {}
        for event in self._events:
            data = event.data
            if event.type == "user/message":
                rows[data["run_id"]] = {"id": data["run_id"], "question": data["question"],
                                        "log_text": data.get("log_text"), "state": "running", "result": None}
            elif event.type == "answer/committed" and data["run_id"] in rows:
                rows[data["run_id"]].update(state="finished", result=deepcopy(data.get("result")))
        return list(rows.values())

    def history_jobs(self):
        visible = {m["run_id"] for m in self.derive_messages(roles={"user"}) if
                   m["role"] == "user" and self._events[m["event_seq"]].type == "user/message"}
        return [row for row in self.turn_jobs() if row["id"] in visible]

    def prior_tool_messages(self, current_run, source_ids=None):
        allowed = set(source_ids) if source_ids is not None else {j["id"] for j in self.history_jobs()}
        messages = self.derive_messages()
        return [m for m in messages if m["run_id"] != current_run and m["run_id"] in allowed and
                (m["role"] == "tool" or (m["role"] == "assistant" and any(b["type"] == "tool-call" for b in m["content"])))]

    def summary_material(self, current_run, recent_turns=2, max_turns=64):
        jobs = [j for j in self.turn_jobs() if j["id"] != current_run]
        previous = self.latest_summary()
        covered = previous.covered_through_sequence if previous else 0
        target = max(covered, len(jobs) - recent_turns)
        selected = jobs[covered:target][:max_turns]
        if not selected:
            return None
        rows = []
        for job in selected:
            result = job.get("result") or {}
            rows.append({"turn_id": job["id"], "question": job["question"], "log_text": job.get("log_text"),
                         "answer": result.get("answer"), "execution_status": result.get("execution_status") or job["state"],
                         "answer_status": result.get("answer_status"), "historical_locations": result.get("citations", [])})
        ids = list(dict.fromkeys((previous.source_turn_ids if previous else []) + [r["turn_id"] for r in rows]))
        return {"turns": rows, "previous_summary": previous.model_dump() if previous else None,
                "source_turn_ids": ids, "covered_through_sequence": covered + len(rows),
                "base_revision": previous.revision if previous else None,
                "session_messages": self.prior_tool_messages(current_run, [r["turn_id"] for r in rows])}

    def prune_observations(self, run_id, observations, registry_fingerprint):
        by_id = {o.call.call_id: o for o in observations}
        for seq in list(self._nodes):
            event = self._events[seq]
            if event.type != "tool/result" or event.data["run_id"] != run_id:
                continue
            old = event.data["observation"]
            projected = by_id.get(old["call"]["call_id"])
            if projected is None or projected.model_dump() == old:
                continue
            data = {**event.data, "observation": projected.model_dump(), "raw_outcome": None}
            self.append("context/prune", {"run_id": run_id, "source_seq": seq,
                "before_sha256": hashlib.sha256(encode(old).encode()).hexdigest(),
                "after_sha256": hashlib.sha256(encode(projected.model_dump()).encode()).hexdigest(),
                "registry_fingerprint": registry_fingerprint, "reason": "request_pressure"})
            self.append("tool/result", data, surface_op={"op": "replace", "start_seq": seq, "end_seq": seq}, source_event_seqs=[seq])

    def runtime_memory(self, run_id):
        return next((deepcopy(self._events[n].data["memory"]) for n in reversed(self._nodes)
                     if self._events[n].type == "context/step_checkpoint" and self._events[n].data["run_id"] == run_id), None)

    def step_material(self, run_id, keep_batches=2):
        messages = [m for m in self.derive_messages(run_id=run_id) if
                    self._events[m["event_seq"]].type in {"assistant/message", "tool/result", "context/step_checkpoint"}]
        batches = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
        if len(batches) <= keep_batches:
            return None
        prefix = messages[:batches[-keep_batches]]
        self.validate_tool_pairs(prefix)
        return {"source_turn_id": run_id, "source_event_seqs": [m["event_seq"] for m in prefix],
                "messages": prefix, "previous_memory": self.runtime_memory(run_id)}

    def apply_step_memory(self, run_id, material, result, generation_id, registry_fingerprint):
        seqs = material["source_event_seqs"]
        call_ids = [b["call_id"] for m in material["messages"] for b in m["content"] if b["type"] == "tool-call"]
        memory = {"result": result.model_dump(), "source_event_seqs": seqs, "covered_call_ids": call_ids,
                  "source_sha256": hashlib.sha256(encode(material).encode()).hexdigest(),
                  "generation_id": generation_id, "registry_fingerprint": registry_fingerprint,
                  "not_source_evidence": True}
        self.append("context/step_checkpoint", {"run_id": run_id, "memory": memory},
                    surface_op={"op": "replace", "start_seq": seqs[0], "end_seq": seqs[-1]}, source_event_seqs=seqs)
        return call_ids


class SessionEventStore:
    def __init__(self, conversations):
        self.conversations = conversations
        with conversations.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS session_events(session_id TEXT NOT NULL,seq INTEGER NOT NULL,body TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(session_id,seq))")

    def load(self, session_id, db=None, *, event_count=None):
        if db is None:
            with self.conversations.connect() as connection:
                return self.load(session_id, connection, event_count=event_count)
        binding = self.conversations.get_session(session_id, db)
        if event_count is not None and (type(event_count) is not int or event_count < 0):
            raise ValueError("Invalid session event watermark")
        rows = db.execute("SELECT body,digest FROM session_events WHERE session_id=? AND seq<? ORDER BY seq", (session_id, event_count if event_count is not None else 2**63 - 1)).fetchall()
        if event_count is not None and len(rows) != event_count:
            raise ValueError("Missing session checkpoint events")
        events = []
        for raw, digest in rows:
            if hashlib.sha256(raw.encode()).hexdigest() != digest:
                raise ValueError("Session event integrity failure")
            events.append(json.loads(raw))
        return ConversationSession(session_id, binding["workspace"]["version"], events)

    def save(self, session, db):
        binding = self.conversations.get_session(session.session_id, db)
        current = db.execute("SELECT seq,body,digest FROM session_events WHERE session_id=? ORDER BY seq", (session.session_id,)).fetchall()
        events = session.events()
        if binding["workspace"]["version"] != session.workspace_version or len(current) > len(events):
            raise ValueError("Stale session event prefix")
        for index, (seq, raw, digest) in enumerate(current):
            if seq != index or hashlib.sha256(raw.encode()).hexdigest() != digest or digest != session._digests[index]:
                raise ValueError("Stale session event prefix or integrity failure")
        for event in events[len(current):]:
            raw = encode(event)
            db.execute("INSERT INTO session_events VALUES(?,?,?,?)", (session.session_id, event["seq"], raw, hashlib.sha256(raw.encode()).hexdigest()))
