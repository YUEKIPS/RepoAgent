"""Transient bounded projections; persistent terminal results remain authoritative."""

import threading
import time
from collections import deque


class StreamBroker:
    def __init__(self, capacity=256, draft_limit=100000):
        self.capacity, self.draft_limit = capacity, draft_limit
        self.condition = threading.Condition()
        self.runs = {}

    def publish(self, run_id, event):
        with self.condition:
            state = self.runs.setdefault(run_id, {"sequence": 0, "revision": 0, "draft": "", "events": deque(maxlen=self.capacity), "terminal": False})
            kind = event["event"]
            revision = event.get("candidate_revision", state["revision"])
            if revision < state["revision"]:
                return
            if revision > state["revision"]:
                state.update(revision=revision, draft="")
            if kind == "draft_delta":
                state["draft"] = (state["draft"] + event.get("text", ""))[:self.draft_limit]
            if kind in {"published", "failed"}:
                state["terminal"] = True
            state["sequence"] += 1
            item = event | {"run_id": run_id, "sequence": state["sequence"], "candidate_revision": revision}
            state["events"].append(item)
            # Only live projections need memory; finished projections are bounded.
            if len(self.runs) > 32:
                for key in list(self.runs):
                    if key != run_id and self.runs[key]["terminal"]:
                        del self.runs[key]
                        break
            self.condition.notify_all()
            return item

    def snapshot(self, run_id):
        with self.condition:
            state = self.runs.get(run_id)
            if not state:
                return {"event": "snapshot", "run_id": run_id, "sequence": 0, "candidate_revision": 0, "text": ""}
            return {"event": "snapshot", "run_id": run_id, "sequence": state["sequence"], "candidate_revision": state["revision"], "text": state["draft"], "terminal": state["terminal"]}

    def subscribe(self, run_id, after=0):
        deadline = time.monotonic() + 25
        first = True
        while time.monotonic() < deadline:
            with self.condition:
                state = self.runs.get(run_id)
                if first or (state and state["events"] and after < state["events"][0]["sequence"] - 1):
                    batch = [self.snapshot(run_id)]
                else:
                    batch = [e for e in state["events"] if e["sequence"] > after] if state else []
                first = False
                terminal = bool(state and state["terminal"])
                if not batch and not terminal:
                    self.condition.wait(timeout=1)
            for event in batch:
                after = event["sequence"]
                yield event
            if terminal:
                return
            if not batch:
                yield {"event": "heartbeat", "sequence": after}
