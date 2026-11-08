"""G3批次预检与顺序配对运行；次数、容量和发送权由执行器控制。"""

import hashlib
import json
import os
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from pydantic import Field

from repoagent.evaluation.dataset import Question, StrictModel, jsonl
from repoagent.execution.agent import run_agent
from repoagent.execution.baseline import run_baseline
from repoagent.execution.contracts import ExecutionPolicy, Query, Workspace
from repoagent.execution.limits import Limits
from repoagent.execution.model_session import ModelSession
from repoagent.execution.runtime import PROTOCOL_VERSION, Execution, PreparedContext
from repoagent.execution.usage_store import UsageStore
from repoagent.models.base import ModelBudgetError, ModelCancelledError
from repoagent.models.config import ModelConfigSnapshot
from repoagent.models.factory import create_adapter
from repoagent.models.mock import ScriptedModel
from repoagent.snapshots.repository import verify_snapshot

ROOT = Path(__file__).resolve().parents[4]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def save(path, value):
    """同目录替换；已有结果由清单摘要验证，调用方控制覆盖时机。"""
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temp = Path(stream.name)
    # Windows编辑器/扫描器可能短暂占用目标。仅重试原子文件替换，不重发模型请求。
    for attempt in range(6):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.025 * (attempt + 1))


class BatchConfig(StrictModel):
    schema_version: Literal[3] = 3
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    stage: Literal["mock", "debug", "formal"]
    dataset: str = "eval/v0.1"
    question_ids: list[str] = Field(min_length=1)
    rounds: int = Field(default=3, ge=1, le=3)
    snapshots: dict[str, str]
    limits: Limits = Field(default_factory=Limits)
    # 当前策略参与封存与配对复现。
    policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    model_config_path: str | None = None
    usage_ledger_path: str | None = None
    capability_cache_path: str | None = None
    mock_scripts: dict[str, str] = Field(default_factory=dict)
    frozen_manifest_sha256: str | None = None
    frozen_manifest_path: str | None = None


def validate_formal_parameters(c, config):
    """v3显式列全参数；原生思考及输出额度固定后再封存源码与配置。"""
    if c.mock_scripts:
        raise ValueError("Formal protocol does not allow mock scripts")
    if c.schema_version != 3:
        raise ValueError("New formal execution requires batch config v3")
    if set(config.get("limits", {})) != set(Limits.model_fields) or set(
        config.get("policy", {})
    ) != set(ExecutionPolicy.model_fields):
        raise ValueError(
            "Formal v3 requires explicit complete limits and policy before sealing"
        )
    if c.policy.native_thinking == "default" or any(
        c.limits.output_for(stage) is None for stage in ("plan", "act", "answer")
    ):
        raise ValueError(
            "Formal v3 requires explicit reproducible native thinking and output limits"
        )
    limits = c.limits.model_dump()
    for key in (
        "max_model_requests",
        "max_tool_calls",
        "deadline_seconds",
        "max_input_estimate",
        "evidence_estimate",
        "top_k",
        "initial_max_reads",
    ):
        if limits[key] != Limits().model_dump()[key]:
            raise ValueError(
                "Formal v3 preserves reviewed input, action and deadline limits"
            )


def preflight(config, *, sealing=False):
    """只解析公开问题；对整个题包校验摘要但不解析评分材料。"""
    c = BatchConfig.model_validate(config)
    folder = ROOT / c.dataset
    meta = read(folder / "dataset.json")
    for name, expected in meta["files_sha256"].items():
        path = (folder / name).resolve()
        if (
            not path.is_relative_to(folder.resolve())
            or digest(path.read_bytes()) != expected
        ):
            raise ValueError("Dataset digest mismatch")
    questions = {}
    for split in ("debug", "unanswerable-debug", "validation"):
        for q in jsonl(folder / "questions" / f"{split}.jsonl", Question):
            if q.question_id in questions:
                raise ValueError("Duplicate question")
            questions[q.question_id] = (q, split)
    if (
        len(set(c.question_ids)) != len(c.question_ids)
        or not set(c.question_ids) <= questions.keys()
    ):
        raise ValueError("Unknown or duplicate selected question")
    if c.stage != "formal" and any(
        questions[q][1] == "validation" for q in c.question_ids
    ):
        raise ValueError("Validation questions are excluded from debugging")
    if c.stage == "formal":
        if meta.get("status") != "frozen":
            raise ValueError(
                "Formal dataset requires completed independent review and freeze"
            )
        from repoagent.evaluation.review import verify_frozen

        verify_frozen(folder)
        selected_validation = {
            q.question_id for q, split in questions.values() if split == "validation"
        }
        if (
            set(c.question_ids) != selected_validation
            or len(selected_validation) != 20
            or c.rounds != 3
        ):
            raise ValueError(
                "Formal plan requires all 20 validation questions and three rounds"
            )
        validate_formal_parameters(c, config)
    manifests = {r: verify_snapshot(ROOT / p) for r, p in c.snapshots.items()}
    selected = []
    for qid in c.question_ids:
        q, _ = questions[qid]
        if q.repo_id not in manifests or manifests[q.repo_id].commit != q.commit:
            raise ValueError("Question snapshot binding mismatch")
        selected.append(q.model_dump())
    sources = {}
    for p in sorted((ROOT / "python/src/repoagent").rglob("*.py")):
        sources[p.relative_to(ROOT).as_posix()] = digest(p.read_bytes())
    files = (
        list(c.mock_scripts.values()) if c.stage == "mock" else [c.model_config_path]
    )
    if c.stage != "mock" and c.capability_cache_path:
        files.append(c.capability_cache_path)
    if c.stage != "mock" and (
        None in files
        or not c.usage_ledger_path
        or not (ROOT / c.usage_ledger_path).is_file()
    ):
        raise ValueError(
            "Live batch requires an existing token usage store and model config v3"
        )
    for name in files:
        if name is None:
            raise ValueError("Missing configuration")
        sources[name] = digest((ROOT / name).read_bytes())
    if c.stage == "mock" and not set(c.question_ids) <= c.mock_scripts.keys():
        raise ValueError("Missing per-question mock script")
    if c.usage_ledger_path and any(
        (ROOT / c.usage_ledger_path).resolve().is_relative_to((ROOT / path).resolve())
        for path in c.snapshots.values()
    ):
        raise ValueError("Ledger must not modify an imported snapshot")
    ledger_identity = None
    if c.stage != "mock":
        ledger = UsageStore(ROOT / c.usage_ledger_path)
        cfg = ModelConfigSnapshot.load(
            ROOT / c.model_config_path,
            capability_cache=ROOT / c.capability_cache_path
            if c.capability_cache_path
            else None,
        ).create_config()
        if cfg.schema_version != 3:
            raise ValueError("Live execution requires model config v3")
        cfg.capabilities.thinking_body(c.policy.native_thinking)
        for stage in ("plan", "act", "answer"):
            cfg.capabilities.output_for(c.limits.output_for(stage))
        ledger_identity = ledger.summary()["batch_id"]
    normalized_config = c.model_dump()
    normalized_config["frozen_manifest_path"] = None
    normalized_config["frozen_manifest_sha256"] = None
    identity = {
        "config": normalized_config,
        "ledger_batch_id": ledger_identity,
        "runtime_versions": {
            "python": sys.version,
            **{
                name: version(name)
                for name in (
                    "pydantic",
                    "tree-sitter",
                    "tree-sitter-java",
                    "tree-sitter-typescript",
                    "tree-sitter-javascript",
                    "openai",
                    "httpx2",
                )
            },
        },
        "execution_protocol": PROTOCOL_VERSION,
        "dataset_sha256": digest((folder / "dataset.json").read_bytes()),
        "sources": sources,
        "snapshots": {r: m.model_dump(mode="json") for r, m in manifests.items()},
        "questions": selected,
    }
    identity["identity_sha256"] = digest(json.dumps(identity, sort_keys=True).encode())
    if c.stage == "formal":
        from repoagent.evaluation.protocol import code_revision, verify_seal

        identity["project_commit"] = code_revision()
        identity.pop("identity_sha256")
        identity["identity_sha256"] = digest(
            json.dumps(identity, sort_keys=True).encode()
        )
        if not sealing:
            verify_seal(c, identity)
    return c, identity


def run_batch(config, output):
    c, identity = preflight(config)
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / "work").resolve()) or any(
        output.is_relative_to((ROOT / p).resolve())
        or (ROOT / p).resolve().is_relative_to(output)
        for p in c.snapshots.values()
    ):
        raise ValueError("Batch output must be in work outside snapshots")
    if c.usage_ledger_path and (ROOT / c.usage_ledger_path).resolve().is_relative_to(
        output
    ):
        raise ValueError("Ledger must be separate from batch output")
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        manifest_path = output / "manifest.json"
        if manifest_path.exists():
            state = read(manifest_path)
            if state["identity"] != identity:
                raise ValueError("Resume identity mismatch")
            if state.get("needs_review"):
                raise ValueError(
                    "needs_review: unresolved dispatch state is retained across resume"
                )
            if state.get("active"):
                raise ValueError(
                    "needs_review: interrupted dispatch; do not automatically resend"
                )
            for filename in output.glob("*.json"):
                if (
                    filename.name != "manifest.json"
                    and filename.name not in state["artifacts"]
                ):
                    raise ValueError("Untracked batch artifact requires review")
            for name, expected in state["artifacts"].items():
                if (
                    Path(name).name != name
                    or digest((output / name).read_bytes()) != expected
                ):
                    raise ValueError("Saved artifact digest mismatch")
        else:
            if list(output.glob("*.json")):
                raise ValueError("Untracked batch artifacts")
            state = {
                "schema_version": 1,
                "identity": identity,
                "active": None,
                "artifacts": {},
                "runs": {},
            }
            save(manifest_path, state)
        budget = None
        if c.stage != "mock":
            model = ModelConfigSnapshot.load(
                ROOT / c.model_config_path,
                capability_cache=ROOT / c.capability_cache_path
                if c.capability_cache_path
                else None,
            )
            if model.source_sha256 != identity["sources"][c.model_config_path]:
                raise ValueError("Model configuration changed after preflight")
            if (
                c.capability_cache_path
                and digest((ROOT / c.capability_cache_path).read_bytes())
                != identity["sources"][c.capability_cache_path]
            ):
                raise ValueError("Capability cache changed after preflight")
            budget = UsageStore(ROOT / c.usage_ledger_path)
            unresolved_before = budget.summary()["unresolved_requests"]
            if budget.summary().get("halted"):
                raise ValueError("Ledger halted")

        def session(qid, preparation=False):
            if c.stage == "mock":
                script_bytes = (ROOT / c.mock_scripts[qid]).read_bytes()
                if digest(script_bytes) != identity["sources"][c.mock_scripts[qid]]:
                    raise ValueError("Mock script changed after preflight")
                script = json.loads(script_bytes)
                return ModelSession(
                    ScriptedModel(script if preparation else script[1:])
                )
            return ModelSession(create_adapter(model.create_config()), budget)

        def persist(name, value):
            save(output / name, value)
            state["artifacts"][name] = digest((output / name).read_bytes())
            save(manifest_path, state)

        def stop_for_review(reason):
            state["needs_review"] = reason
            save(manifest_path, state)
            raise ValueError(
                "needs_review: unresolved request usage must be reviewed before resume"
            )

        for qi, qdata in enumerate(identity["questions"]):
            q = Question.model_validate(qdata)
            snapshot = ROOT / c.snapshots[q.repo_id]
            manifest = verify_snapshot(snapshot)
            query = Query(
                workspace=Workspace.bind({q.repo_id: manifest}),
                question=q.question,
                log_text=q.log_text,
                policy=c.policy,
            )
            for number in range(1, c.rounds + 1):
                pair = f"{qi:03d}-r{number}"
                modes = (
                    ["baseline", "agent"]
                    if (qi + number) % 2
                    else ["agent", "baseline"]
                )
                if all(f"{pair}-{m}" in state["runs"] for m in modes):
                    continue
                prep_name = pair + "-prepared.json"
                failure_name = pair + "-preparation-failure.json"
                if prep_name in state["artifacts"]:
                    prepared = PreparedContext.model_validate(read(output / prep_name))
                elif failure_name in state["artifacts"]:
                    prepared = None
                else:
                    if budget:
                        summary = budget.summary()
                        if (
                            summary.get("halted")
                            or summary["unresolved_requests"] > unresolved_before
                        ):
                            raise ValueError(
                                "Batch ledger requires review before preparation"
                            )
                    state["active"] = pair + "-prepare"
                    save(manifest_path, state)
                    executor = Execution(
                        {q.repo_id: snapshot}, query, session(q.question_id, True), c.limits
                    )
                    preparation_started = time.monotonic()
                    try:
                        prepared = executor.prepare()
                    except Exception as error:
                        # 保留失败用量/轨迹；不打印可能含服务数据的异常原文。
                        status = (
                            "BUDGET_EXCEEDED"
                            if isinstance(error, ModelBudgetError)
                            else "CANCELLED"
                            if isinstance(error, ModelCancelledError)
                            else "FAILED"
                        )
                        failure = executor.failed(status, "shared_preparation_failed")
                        failure.trace.append(
                            {
                                "event": "shared_preparation_failed",
                                "duration_seconds": time.monotonic()
                                - preparation_started,
                            }
                        )
                        persist(failure_name, failure.model_dump(mode="json"))
                        prepared = None
                    else:
                        persist(prep_name, prepared.model_dump(mode="json"))
                    state["active"] = None
                    save(manifest_path, state)
                for mode in modes:
                    key = f"{pair}-{mode}"
                    if key in state["runs"]:
                        continue
                    if budget and prepared is not None:
                        summary = budget.summary()
                        if (
                            summary.get("halted")
                            or summary["unresolved_requests"] > unresolved_before
                        ):
                            raise ValueError("Batch ledger requires review")
                    state["active"] = key
                    save(manifest_path, state)
                    started = time.monotonic()
                    if prepared is None:
                        result = read(output / failure_name)
                    else:
                        runner = run_baseline if mode == "baseline" else run_agent
                        result = runner(
                            {q.repo_id: snapshot},
                            query,
                            session(q.question_id),
                            c.limits,
                            prepared=prepared,
                        ).model_dump(mode="json")
                    preparation_duration = (
                        prepared.preparation_seconds
                        if prepared
                        else next(
                            (
                                entry["duration_seconds"]
                                for entry in result["trace"]
                                if entry.get("event") == "shared_preparation_failed"
                                and "duration_seconds" in entry
                            ),
                            None,
                        )
                    )
                    record = {
                        "run_id": key,
                        "attempt_id": key + "-a1",
                        "pair_id": pair,
                        "question_id": q.question_id,
                        "repo_id": q.repo_id,
                        "category": q.category,
                        "round": number,
                        "mode": mode,
                        "result": result,
                        "duration_including_preparation_seconds": time.monotonic()
                        - started
                        + preparation_duration
                        if preparation_duration is not None
                        else None,
                    }
                    persist(key + ".json", record)
                    state["runs"][key] = key + ".json"
                    state["active"] = None
                    save(manifest_path, state)
                    if (
                        budget
                        and prepared is not None
                        and (
                            budget.summary().get("halted")
                            or budget.summary()["unresolved_requests"]
                            > unresolved_before
                        )
                    ):
                        stop_for_review("ledger_halted_or_new_unresolved_request")
                if budget and prepared is None:
                    summary = budget.summary()
                    if (
                        summary.get("halted")
                        or summary["unresolved_requests"] > unresolved_before
                    ):
                        stop_for_review("shared_preparation_failed_with_unknown_usage")
        return {
            "saved_runs": len(state["runs"]),
            "expected_runs": len(c.question_ids) * c.rounds * 2,
            "simulated": c.stage == "mock",
            "formal": c.stage == "formal",
        }
    finally:
        lock.unlink()
