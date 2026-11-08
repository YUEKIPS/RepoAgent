"""非金额请求记录：持久发送权、服务请求去重和token归因。

新表与历史费用账本隔离；未知用量保留未知，不做价格、金额或消费预留计算。
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from repoagent.models.base import ModelBudgetError
from repoagent.storage import guard_database


@dataclass(frozen=True)
class RequestTicket:
    """绑定批次、任务与请求摘要的一次发送票据，不代表金额预留。"""

    attempt_id: str
    task_id: str
    batch_id: str
    wire_sha256: str


class UsageStore:
    """用独立SQLite文件持久记录请求；重启不清除在途或未知状态。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        guard_database(self.path, "usage-current-v1")
        with self.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage_meta (name TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage_calls (id TEXT PRIMARY KEY,state TEXT NOT NULL,wire TEXT NOT NULL,request_key TEXT UNIQUE,fingerprint TEXT,record TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage_attribution (task_id TEXT NOT NULL,call_id TEXT NOT NULL,PRIMARY KEY(task_id,call_id))"
            )
            for key, value in (
                ("schema_version", "1"),
                ("batch_id", str(uuid4())),
                ("halted", ""),
            ):
                db.execute(
                    "INSERT OR IGNORE INTO usage_meta VALUES (?,?)", (key, value)
                )
            if db.execute(
                "SELECT value FROM usage_meta WHERE name='schema_version'"
            ).fetchone() != ("1",):
                raise ValueError("Unsupported token usage store version")
            self.batch_id = db.execute(
                "SELECT value FROM usage_meta WHERE name='batch_id'"
            ).fetchone()[0]

    @contextmanager
    def transaction(self):
        """写事务原子串行，异常回滚；连接只在本次操作使用。"""
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def bind_identity(self, identity):
        """固定非敏感端点/账户范围/模型身份，不能将同批记录换到其他服务。"""
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        with self.transaction() as db:
            row = db.execute(
                "SELECT value FROM usage_meta WHERE name='service_identity'"
            ).fetchone()
            if row and row[0] != encoded:
                raise ModelBudgetError(code="usage_service_identity_mismatch")
            db.execute(
                "INSERT OR IGNORE INTO usage_meta VALUES ('service_identity',?)",
                (encoded,),
            )

    def reserve(self, task_id, model, input_bound, output_bound, wire_sha256):
        """登记未发送请求；沿用调用签名以便执行器迁移，不计算任何金额。"""
        ticket = RequestTicket(str(uuid4()), task_id, self.batch_id, wire_sha256)
        with self.transaction() as db:
            if db.execute(
                "SELECT value FROM usage_meta WHERE name='halted'"
            ).fetchone()[0]:
                raise ModelBudgetError(code="usage_store_halted")
            db.execute(
                "INSERT INTO usage_calls(id,state,wire) VALUES (?,'reserved',?)",
                (ticket.attempt_id, wire_sha256),
            )
            db.execute(
                "INSERT INTO usage_attribution VALUES (?,?)",
                (task_id, ticket.attempt_id),
            )
        return ticket

    def _verify(self, db, ticket, task_id, wire_sha256):
        row = db.execute(
            "SELECT state,wire FROM usage_calls WHERE id=?", (ticket.attempt_id,)
        ).fetchone()
        attributed = db.execute(
            "SELECT 1 FROM usage_attribution WHERE task_id=? AND call_id=?",
            (task_id, ticket.attempt_id),
        ).fetchone()
        if (
            ticket.task_id != task_id
            or ticket.batch_id != self.batch_id
            or ticket.wire_sha256 != wire_sha256
            or not attributed
            or row != ("reserved", wire_sha256)
        ):
            raise ModelBudgetError(code="invalid_usage_ticket")

    def verify(self, ticket, task_id, wire_sha256):
        with self.transaction() as db:
            self._verify(db, ticket, task_id, wire_sha256)

    def claim(self, ticket, task_id, wire_sha256):
        """原子领取一次发送权，重复消费或并发领取均拒绝。"""
        with self.transaction() as db:
            self._verify(db, ticket, task_id, wire_sha256)
            if db.execute(
                "SELECT value FROM usage_meta WHERE name='halted'"
            ).fetchone()[0]:
                raise ModelBudgetError(code="usage_store_halted")
            db.execute(
                "UPDATE usage_calls SET state='dispatching' WHERE id=?",
                (ticket.attempt_id,),
            )

    def release_not_sent(self, ticket):
        """只可释放从未领取的票据，不能将可能已发送请求改成零消耗。"""
        with self.transaction() as db:
            self._verify(db, ticket, ticket.task_id, ticket.wire_sha256)
            db.execute(
                "UPDATE usage_calls SET state='not_sent' WHERE id=?",
                (ticket.attempt_id,),
            )

    @staticmethod
    def _fingerprint(record):
        keys = (
            "provider",
            "requested_model",
            "returned_model",
            "wire_sha256",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_hit_input_tokens",
            "cache_miss_input_tokens",
            "request_id",
            "response_id",
        )
        return json.dumps({key: getattr(record, key) for key in keys}, sort_keys=True)

    def settle(self, ticket, record, dispatched):
        """记录服务用量/未知及物理请求身份；相同ID但不一致内容会持久停止。"""
        state = (
            "not_sent"
            if not dispatched
            else "reported"
            if record.input_tokens is not None and record.output_tokens is not None
            else "unknown"
        )
        key = record.request_id or record.response_id
        fingerprint = self._fingerprint(record)
        duplicate, conflict = None, False
        with self.transaction() as db:
            old = db.execute(
                "SELECT state,wire FROM usage_calls WHERE id=?", (ticket.attempt_id,)
            ).fetchone()
            if (
                ticket.batch_id != self.batch_id
                or old
                not in {
                    ("reserved", ticket.wire_sha256),
                    ("dispatching", ticket.wire_sha256),
                }
                or record.wire_sha256 != ticket.wire_sha256
            ):
                raise ModelBudgetError(code="invalid_usage_ticket")
            previous = (
                db.execute(
                    "SELECT id,fingerprint FROM usage_calls WHERE request_key=?", (key,)
                ).fetchone()
                if key and state == "reported"
                else None
            )
            if previous:
                if previous[1] == fingerprint:
                    duplicate = previous[0]
                    db.execute(
                        "DELETE FROM usage_attribution WHERE call_id=?",
                        (ticket.attempt_id,),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO usage_attribution VALUES (?,?)",
                        (ticket.task_id, duplicate),
                    )
                    state = "duplicate"
                else:
                    state, conflict = "unknown", True
                    db.execute(
                        "UPDATE usage_meta SET value='service_request_identity_conflict' WHERE name='halted'"
                    )
            record.usage_attempt_id = duplicate or ticket.attempt_id
            record.usage_batch_id = self.batch_id
            record.usage_status = state
            db.execute(
                "UPDATE usage_calls SET state=?,request_key=?,fingerprint=?,record=? WHERE id=?",
                (
                    state,
                    key if state == "reported" else None,
                    fingerprint,
                    record.model_dump_json(exclude_none=True),
                    ticket.attempt_id,
                ),
            )
        if conflict:
            raise ModelBudgetError(
                code="service_request_identity_conflict", record=record
            )

    def inherit(self, task_id, records):
        """共享准备只继承同批且内容一致的真实请求，归因不增加物理调用。"""
        with self.transaction() as db:
            for record in records:
                row = db.execute(
                    "SELECT fingerprint FROM usage_calls WHERE id=?",
                    (record.usage_attempt_id,),
                ).fetchone()
                if (
                    record.usage_batch_id != self.batch_id
                    or not row
                    or row[0] != self._fingerprint(record)
                ):
                    raise ModelBudgetError(code="prepared_usage_mismatch")
                db.execute(
                    "INSERT OR IGNORE INTO usage_attribution VALUES (?,?)",
                    (task_id, record.usage_attempt_id),
                )

    def summary(self, task_id=None):
        """返回已知累计与统计完整性；未知请求不会伪装为零用量。"""
        with self.transaction() as db:
            sql = "SELECT c.state,c.record FROM usage_calls c"
            rows = db.execute(
                sql
                + (
                    " JOIN usage_attribution a ON a.call_id=c.id WHERE a.task_id=?"
                    if task_id
                    else ""
                ),
                (task_id,) if task_id else (),
            ).fetchall()
            halted = (
                db.execute(
                    "SELECT value FROM usage_meta WHERE name='halted'"
                ).fetchone()[0]
                or None
            )
        rows = [
            (state, json.loads(raw) if raw else {})
            for state, raw in rows
            if state != "duplicate"
        ]
        pending = sum(
            state in {"reserved", "dispatching", "unknown"} for state, _ in rows
        )
        return {
            "batch_id": self.batch_id,
            "registered_attempts": len(rows),
            "unique_requests": sum(
                state not in {"not_sent", "reserved"} for state, _ in rows
            ),
            "reported_input_tokens": sum(
                r.get("input_tokens") or 0 for state, r in rows if state != "not_sent"
            ),
            "reported_output_tokens": sum(
                r.get("output_tokens") or 0 for state, r in rows if state != "not_sent"
            ),
            "reasoning_tokens": sum(
                r.get("reasoning_tokens") or 0
                for state, r in rows
                if state != "not_sent"
            ),
            "requests_without_reasoning_usage": sum(
                r.get("reasoning_tokens") is None
                for state, r in rows
                if state not in {"not_sent", "reserved"}
            ),
            "unresolved_requests": pending,
            "usage_complete": pending == 0,
            "halted": halted,
            "basis": "provider_reported_tokens_not_estimated_cost",
        }

    def configuration(self):
        return {
            "schema_version": "token-usage-v1",
            "batch_id": self.batch_id,
            "identity": "bound_per_service_account_model",
        }

    def release_reserved_task(self, task_id):
        """Only unclaimed reservations are provably not sent; recovery holds run ownership."""
        with self.transaction() as db:
            db.execute("UPDATE usage_calls SET state='not_sent' WHERE state='reserved' AND id IN (SELECT call_id FROM usage_attribution WHERE task_id=?)", (task_id,))
