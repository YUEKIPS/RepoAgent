"""评分包导出与导入；只在此离线阶段加载rubrics。"""

from collections import Counter, defaultdict
from math import ceil
from pathlib import Path
from statistics import median

from pydantic import Field

from repoagent.evaluation.batch import ROOT, digest, read, save
from repoagent.evaluation.dataset import Rubric, StrictModel, jsonl
from repoagent.execution.contracts import Result


class Judgment(StrictModel):
    met: bool
    reason: str = Field(min_length=1)


class CitationJudgment(StrictModel):
    locatable: bool
    supports_claim: bool
    reason: str = Field(min_length=1)


class Score(StrictModel):
    run_id: str
    result_sha256: str
    scorer: str = Field(min_length=1)
    points: dict[str, Judgment]
    citations: dict[str, CitationJudgment]
    key_claims_covered: bool
    critical_errors: list[str]
    refusal_correct: bool | None = None


def load_batch(folder):
    folder = Path(folder)
    state = read(folder / "manifest.json")
    records = {}
    for name, expected in state["artifacts"].items():
        if Path(name).name != name or digest((folder / name).read_bytes()) != expected:
            raise ValueError("Batch artifact mismatch")
    for key, name in state["runs"].items():
        if name not in state["artifacts"]:
            raise ValueError("Missing result digest")
        record = read(folder / name)
        Result.model_validate(record["result"])
        if record["run_id"] != key or record["mode"] not in {"baseline", "agent"}:
            raise ValueError("Result record identity mismatch")
        config = state["identity"]["config"]
        if (
            record["question_id"] not in config["question_ids"]
            or not 1 <= record["round"] <= config["rounds"]
        ):
            raise ValueError("Unexpected question/round")
        qi = config["question_ids"].index(record["question_id"])
        if key != f"{qi:03d}-r{record['round']}-{record['mode']}":
            raise ValueError("Unexpected run slot")
        records[key] = record
    return state, records


def export_scores(folder, output):
    state, runs = load_batch(folder)
    dataset = ROOT / state["identity"]["config"]["dataset"]
    if (
        digest((dataset / "dataset.json").read_bytes())
        != state["identity"]["dataset_sha256"]
    ):
        raise ValueError("Rubric dataset changed")
    meta = read(dataset / "dataset.json")
    rubrics = {}
    for split in ("debug", "unanswerable-debug", "validation"):
        name = f"rubrics/{split}.jsonl"
        if digest((dataset / name).read_bytes()) != meta["files_sha256"][name]:
            raise ValueError("Rubric hash mismatch")
        rubrics.update({r.question_id: r for r in jsonl(dataset / name, Rubric)})
    rows = []
    for key, record in runs.items():
        rubric = rubrics[record["question_id"]]
        result = record["result"]
        rows.append(
            {
                "run_id": key,
                "result_sha256": state["artifacts"][state["runs"][key]],
                "scorer": None,
                "points": {
                    p.point_id: {"met": None, "reason": ""}
                    for p in rubric.required_points
                },
                "citations": {
                    str(i): {"locatable": None, "supports_claim": None, "reason": ""}
                    for i, _ in enumerate(result["citations"])
                },
                "key_claims_covered": None,
                "critical_errors": [],
                "refusal_correct": None,
            }
        )
    if Path(output).exists():
        raise ValueError("Refuse to overwrite score work")
    save(
        output,
        {
            "scores": rows,
            "rubrics": {
                q: r.model_dump()
                for q, r in rubrics.items()
                if q in {v["question_id"] for v in runs.values()}
            },
        },
    )
    return {"exported": len(rows)}


def summarize(folder, score_file, billing_file=None):
    state, runs = load_batch(folder)
    packet = read(score_file)
    rows = packet["scores"]
    if len({r["run_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate scoring row")
    # 可信评分要点从已绑定题集重新读取，不信任用户文件里的rubrics字段。
    config = state["identity"]["config"]
    dataset = ROOT / config["dataset"]
    if (
        digest((dataset / "dataset.json").read_bytes())
        != state["identity"]["dataset_sha256"]
    ):
        raise ValueError("Dataset changed")
    rubrics = {}
    meta = read(dataset / "dataset.json")
    for split in ("debug", "unanswerable-debug", "validation"):
        name = f"rubrics/{split}.jsonl"
        if digest((dataset / name).read_bytes()) != meta["files_sha256"][name]:
            raise ValueError("Rubrics changed")
        rubrics.update({r.question_id: r for r in jsonl(dataset / name, Rubric)})
    passed, refused, citations = {}, {}, Counter()
    citations_by_mode = {m: Counter() for m in ("baseline", "agent")}
    for row in rows:
        if row["run_id"] not in runs:
            raise ValueError("Unknown run score")
        key = row["run_id"]
        if row["result_sha256"] != state["artifacts"][state["runs"][key]]:
            raise ValueError("Stale scoring result hash")
        if row.get("scorer") is None:
            continue
        score = Score.model_validate(row)
        record = runs[key]
        result = record["result"]
        rubric = rubrics[record["question_id"]]
        if set(score.points) != {p.point_id for p in rubric.required_points} or set(
            score.citations
        ) != {str(i) for i, _ in enumerate(result["citations"])}:
            raise ValueError("Point or citation IDs mismatch")
        ok = (
            result["execution_status"] == "SUCCEEDED"
            and result["answer_status"] == rubric.expected_answer_status
        )
        ok = (
            ok
            and all(p.met for p in score.points.values())
            and not score.critical_errors
        )
        ok = (
            ok
            and score.key_claims_covered
            and all(c.locatable and c.supports_claim for c in score.citations.values())
        )
        if rubric.expected_answer_status == "answered":
            ok = ok and bool(result["citations"]) and bool(result["claims"])
        if rubric.expected_answer_status == "insufficient_evidence":
            ok = ok and score.refusal_correct is True
        passed[key] = bool(ok)
        refused[key] = (
            rubric.expected_answer_status == "answered"
            and result["execution_status"] == "SUCCEEDED"
            and result["answer_status"] == "insufficient_evidence"
        )
        for c in score.citations.values():
            citations["total"] += 1
            citations["locatable"] += c.locatable
            citations["supports_claim"] += c.supports_claim
            mode_counts = citations_by_mode[record["mode"]]
            mode_counts["total"] += 1
            mode_counts["locatable"] += c.locatable
            mode_counts["supports_claim"] += c.supports_claim
    groups = defaultdict(list)
    for key, record in runs.items():
        groups[(record["question_id"], record["mode"])].append(key)
    majority = []
    for (qid, mode), keys in sorted(groups.items()):
        complete = len(keys) == 3 and all(k in passed for k in keys)
        majority.append(
            {
                "question_id": qid,
                "mode": mode,
                "majority_pass": sum(passed[k] for k in keys) >= 2
                if complete
                else None,
                "wrong_refusal": sum(refused[k] for k in keys) >= 2
                if complete
                else None,
            }
        )
    paired = {"agent_win": 0, "baseline_win": 0, "tie": 0}
    by_question = defaultdict(dict)
    strata = {}
    for item in majority:
        by_question[item["question_id"]][item["mode"]] = item["majority_pass"]
        record = next(
            r for r in runs.values() if r["question_id"] == item["question_id"]
        )
        for field in ("category", "repo_id"):
            label = field + ":" + record[field] + ":" + item["mode"]
            stat = strata.setdefault(label, {"passed": 0, "reviewed": 0, "pending": 0})
            if item["majority_pass"] is None:
                stat["pending"] += 1
            else:
                stat["reviewed"] += 1
                stat["passed"] += item["majority_pass"]
    for modes in by_question.values():
        if set(modes) != {"agent", "baseline"} or None in modes.values():
            continue
        if modes["agent"] == modes["baseline"]:
            paired["tie"] += 1
        elif modes["agent"]:
            paired["agent_win"] += 1
        else:
            paired["baseline_win"] += 1
    timings = {}
    for mode in ("baseline", "agent"):
        values = sorted(
            r["duration_including_preparation_seconds"]
            for r in runs.values()
            if r["mode"] == mode
            and r.get("duration_including_preparation_seconds") is not None
        )
        timings[mode] = {
            "samples": len(values),
            "median_seconds": median(values) if values else None,
            "p95_seconds": values[ceil(0.95 * len(values)) - 1] if values else None,
        }
    calls = {}
    for record in runs.values():
        for call in record["result"]["usage"]["service_calls"]:
            identity = call.get("usage_attempt_id")
            if identity:
                calls[identity] = call
    report = {
        "planned": len(config["question_ids"]) * config["rounds"] * 2,
        "saved": len(runs),
        "scored": len(passed),
        "pending_scores": len(runs) - len(passed),
        "passed_runs": sum(passed.values()),
        "majority": majority,
        "paired_majority": paired,
        "strata": strata,
        "citations": dict(citations),
        "unique_physical_requests": len(calls),
        "timing": timings,
        "result_hashes": {
            key: state["artifacts"][name] for key, name in state["runs"].items()
        },
        "citations_by_mode": {m: dict(c) for m, c in citations_by_mode.items()},
        "gate_status": "NOT_EVALUATED",
        "formal": config["stage"] == "formal",
        "limitations": [
            "No automatic semantic grading; actual bill unverified; formal freeze/execution not enabled."
        ],
    }

    report["tokens"] = {
        "reported_input_tokens": sum(
            c.get("input_tokens") or 0 for c in calls.values()
        ),
        "reported_output_tokens": sum(
            c.get("output_tokens") or 0 for c in calls.values()
        ),
        "reasoning_tokens": sum(
            c.get("reasoning_tokens") or 0 for c in calls.values()
        ),
        "requests_without_usage": sum(
            c.get("input_tokens") is None or c.get("output_tokens") is None
            for c in calls.values()
        ),
    }
    from repoagent.evaluation.gates import evaluate_gates

    report["gates"] = evaluate_gates(
        report, runs, config, state["identity"]["identity_sha256"], billing_file
    )
    report["gate_status"] = report["gates"]["status"]
    report["limitations"] = [
        "Human judgments and billing provenance require independent audit; hashes only verify binding."
    ]
    return report
