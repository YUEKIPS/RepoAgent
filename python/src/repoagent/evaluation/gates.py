"""需求v0.2正式门槛：完整性、质量与实际费用证据分开判定。"""

from decimal import Decimal, InvalidOperation
from pathlib import Path

from repoagent.evaluation.batch import digest, read


def evaluate_gates(report, records, config, identity_sha256, billing_file=None):
    if config["stage"] != "formal":
        return {
            "status": "NOT_APPLICABLE",
            "reason": "Debug/mock results are not formal evaluation",
        }
    if (
        report["planned"] != 120
        or report["saved"] != 120
        or report["pending_scores"]
        or report["scored"] != 120
    ):
        return {
            "status": "PENDING",
            "reason": "All 120 results and scoring decisions are required",
        }
    strata = report["strata"]

    def successes(category, mode):
        row = strata.get(f"category:{category}:{mode}", {})
        if (
            row.get("pending")
            or row.get("reviewed")
            != {"location": 5, "flow": 6, "exception": 4, "unanswerable": 5}[category]
        ):
            raise ValueError("Formal scoring distribution incomplete")
        return row["passed"]

    majority = report["majority"]
    refusal = sum(r["wrong_refusal"] is True for r in majority if r["mode"] == "agent")
    agent_answerable = sum(
        successes(c, "agent") for c in ("location", "flow", "exception")
    )
    baseline_answerable = sum(
        successes(c, "baseline") for c in ("location", "flow", "exception")
    )
    cites = report["citations_by_mode"].get("agent", {})
    total = cites.get("total", 0)
    quality = {
        "location_4_of_5": successes("location", "agent") >= 4,
        "flow_exception_7_of_10": successes("flow", "agent")
        + successes("exception", "agent")
        >= 7,
        "unanswerable_4_of_5": successes("unanswerable", "agent") >= 4,
        "wrong_refusal_at_most_3": refusal <= 3,
        "flow_net_gain_at_least_2": successes("flow", "agent")
        - successes("flow", "baseline")
        >= 2,
        "answerable_not_decreased": agent_answerable >= baseline_answerable,
        "citation_locatable_100_percent": total > 0
        and cites.get("locatable", 0) == total,
        "citation_support_at_least_90_percent": total > 0
        and Decimal(cites.get("supports_claim", 0)) / total >= Decimal("0.90"),
    }
    durations = [
        r.get("duration_including_preparation_seconds") for r in records.values()
    ]
    time_status = (
        "PENDING"
        if any(d is None for d in durations)
        else "PASS"
        if all(0 <= d <= 300 for d in durations)
        else "FAIL"
    )
    billing = {
        "status": "PENDING",
        "reason": "No actual bill attribution evidence supplied",
    }
    if billing_file:
        data = read(billing_file)
        if (
            data["identity_sha256"] != identity_sha256
            or data["currency"] != "CNY"
            or not data["source_description"].strip()
        ):
            raise ValueError("Billing evidence binding mismatch")
        if digest(Path(data["source_file"]).read_bytes()) != data["source_sha256"]:
            raise ValueError("Actual bill source hash mismatch")
        if set(data["attribution"]) != set(records):
            raise ValueError("Actual bill must cover all attempts including failures")
        amounts = {}
        for key, item in data["attribution"].items():
            if item["result_sha256"] != report["result_hashes"][key]:
                raise ValueError("Actual bill result hash mismatch")
            try:
                value = Decimal(item["actual_attributed_cost"])
            except (InvalidOperation, TypeError):
                raise ValueError("Invalid actual cost") from None
            if not value.is_finite() or value < 0:
                raise ValueError("Actual cost must be finite and nonnegative")
            amounts[key] = value
        sums = {
            m: sum(
                (amounts[k] for k, r in records.items() if r["mode"] == m), Decimal(0)
            )
            for m in ("baseline", "agent")
        }
        ratio_ok = sums["baseline"] == 0 or sums["agent"] <= 3 * sums["baseline"]
        task_ok = None
        physical = Decimal(data["actual_unique_batch_cost"])
        if not physical.is_finite() or physical < 0:
            raise ValueError("Invalid actual batch cost")
        batch_ok = None
        billing = {
            "status": "PASS"
            if ratio_ok and task_ok is not False and batch_ok is not False
            else "FAIL",
            "ratio_ok": ratio_ok,
            "task_caps_ok": task_ok,
            "batch_cap_ok": batch_ok,
            "source_sha256": data["source_sha256"],
            "note": "User-supplied bill attribution; hash verifies binding, not provider authenticity",
        }
    checks_pass = all(quality.values())
    status = (
        "FAIL"
        if not checks_pass or time_status == "FAIL" or billing["status"] == "FAIL"
        else "PASS"
        if time_status == billing["status"] == "PASS"
        else "PENDING"
    )
    return {
        "status": status,
        "quality": {"status": "PASS" if checks_pass else "FAIL", "checks": quality},
        "time": time_status,
        "billing": billing,
        "sample_limitation": "Small frozen dataset; no statistical significance claim",
    }
