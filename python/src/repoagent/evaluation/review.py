"""独立人工复核记录与不可覆盖的题集冻结；不自动生成复核结论。"""

import json
import shutil
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from repoagent.evaluation.batch import ROOT, digest, read, save
from repoagent.evaluation.dataset import (
    SPLITS,
    Question,
    Rubric,
    StrictModel,
    jsonl,
    validate_pack,
)


class ReviewRow(StrictModel):
    question_id: str
    source_sha256: str
    reviewer: str = Field(min_length=1)
    reviewer_kind: Literal["human"]
    independent: Literal[True]
    answerability_checked: Literal[True]
    points_supported: Literal[True]
    forbidden_claims_checked: Literal[True]
    contamination_note: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def nonblank(self):
        if any(
            not value.strip()
            for value in (self.reviewer, self.reason, self.contamination_note)
        ):
            raise ValueError("Human review text fields cannot be blank")
        return self


class Reviews(StrictModel):
    schema_version: Literal[1]
    dataset_sha256: str
    rows: list[ReviewRow]


def inventory(folder):
    rows = {}
    for split in SPLITS:
        qs = {
            q.question_id: q
            for q in jsonl(folder / "questions" / f"{split}.jsonl", Question)
        }
        for r in jsonl(folder / "rubrics" / f"{split}.jsonl", Rubric):
            q = qs[r.question_id]
            payload = {"question": q.model_dump(), "rubric": r.model_dump()}

            source = digest(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            )
            rows[r.question_id] = {"split": split, "source_sha256": source, **payload}
    return rows


def export_reviews(dataset, output):
    folder = (ROOT / dataset).resolve()
    validate_pack(folder, ROOT)
    if Path(output).exists():
        raise ValueError("Review export already exists")
    rows = inventory(folder)
    if Path(output).with_suffix(".sources.json").exists():
        raise ValueError("Review source export already exists")
    # source供阅读；导入时重新读可信题包，不从可编辑说明中读取评分标准。
    save(
        output,
        {
            "schema_version": 1,
            "dataset_sha256": digest((folder / "dataset.json").read_bytes()),
            "rows": [
                {
                    "question_id": qid,
                    "source_sha256": row["source_sha256"],
                    "reviewer": None,
                    "reviewer_kind": None,
                    "independent": None,
                    "answerability_checked": None,
                    "points_supported": None,
                    "forbidden_claims_checked": None,
                    "contamination_note": None,
                    "reason": None,
                }
                for qid, row in rows.items()
            ],
        },
    )
    source_path = Path(output).with_suffix(".sources.json")
    if source_path.exists():
        raise ValueError("Review source export already exists")
    save(source_path, rows)
    return {
        "pending": len(rows),
        "reviewed": 0,
        "note": "Human declarations required; exporter does not review",
    }


def freeze_dataset(dataset, review_file, output, version):
    source = (ROOT / dataset).resolve()
    target = Path(output).resolve()
    if (
        not target.is_relative_to(ROOT.resolve())
        or target == source
        or target.is_relative_to(source)
    ):
        raise ValueError(
            "Freeze destination must be new project directory outside candidate"
        )
    validate_pack(source, ROOT)
    reviews = Reviews.model_validate(read(review_file))
    if reviews.dataset_sha256 != digest((source / "dataset.json").read_bytes()):
        raise ValueError("Review dataset hash mismatch")
    rows = inventory(source)
    submitted = {r.question_id: r for r in reviews.rows}
    if (
        len(submitted) != len(reviews.rows)
        or set(submitted) != set(rows)
        or len(rows) != 32
    ):
        raise ValueError("Exactly 32 unique review declarations required")
    if any(
        r.source_sha256 != rows[qid]["source_sha256"] for qid, r in submitted.items()
    ):
        raise ValueError("Stale review declaration")
    if (
        not version.strip()
        or version == read(source / "dataset.json")["dataset_version"]
    ):
        raise ValueError("A new frozen dataset version is required")
    if target.exists():
        raise ValueError("Refuse to overwrite freeze")
    # 仅复制清单文件，不递归复制外来附加内容或符号链接。
    meta = read(source / "dataset.json")
    for name in meta["files_sha256"]:
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, dest)
    for split in SPLITS:
        path = target / "rubrics" / f"{split}.jsonl"
        records = jsonl(path, Rubric)
        for record in records:
            record.review_status = "reviewed"
        path.write_text(
            "\n".join(r.model_dump_json() for r in records) + "\n",
            encoding="utf8",
            newline="\n",
        )
    meta.update(
        status="frozen",
        dataset_version=version,
        annotation_method="Human review declarations archived; declaration authenticity requires external audit",
    )
    meta["files_sha256"] = {
        name: digest((target / name).read_bytes()) for name in meta["files_sha256"]
    }
    save(target / "dataset.json", meta)
    save(target / "review-declarations.json", reviews.model_dump())
    save(target / "source-review-inventory.json", rows)
    result = validate_pack(target, ROOT)
    save(
        target / "freeze-receipt.json",
        {
            "source_dataset_sha256": reviews.dataset_sha256,
            "source_inventory_sha256": digest(
                (target / "source-review-inventory.json").read_bytes()
            ),
            "review_file_sha256": digest(Path(review_file).read_bytes()),
            "frozen_dataset_sha256": result["dataset_sha256"],
            "review_declarations_sha256": digest(
                (target / "review-declarations.json").read_bytes()
            ),
        },
    )
    return result


def verify_frozen(folder):
    """正式预检验证声明与冻结副本，不将review_status单字段当作充分条件。"""
    report = validate_pack(folder, ROOT)
    if report["status"] != "frozen" or report["human_reviewed_rubrics"] != 32:
        raise ValueError("Formal dataset is not fully reviewed and frozen")
    receipt = read(folder / "freeze-receipt.json")
    declarations = Reviews.model_validate(read(folder / "review-declarations.json"))
    if receipt["frozen_dataset_sha256"] != report["dataset_sha256"] or receipt[
        "review_declarations_sha256"
    ] != digest((folder / "review-declarations.json").read_bytes()):
        raise ValueError("Frozen review receipt mismatch")
    if (
        declarations.dataset_sha256 != receipt["source_dataset_sha256"]
        or len({r.question_id for r in declarations.rows}) != 32
        or len(declarations.rows) != 32
    ):
        raise ValueError("Frozen review declarations incomplete")
    if set(inventory(folder)) != {r.question_id for r in declarations.rows}:
        raise ValueError("Frozen review IDs mismatch")
    originals = read(folder / "source-review-inventory.json")
    if (
        digest((folder / "source-review-inventory.json").read_bytes())
        != receipt["source_inventory_sha256"]
    ):
        raise ValueError("Review source inventory changed")
    current = inventory(folder)
    for row in declarations.rows:
        original = originals[row.question_id]
        payload = {"question": original["question"], "rubric": original["rubric"]}
        if (
            digest(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())
            != row.source_sha256
        ):
            raise ValueError("Review source declaration hash mismatch")
        expected = json.loads(json.dumps(payload))
        expected["rubric"]["review_status"] = "reviewed"
        if (
            current[row.question_id]["question"] != expected["question"]
            or current[row.question_id]["rubric"] != expected["rubric"]
        ):
            raise ValueError("Frozen content no longer matches human reviewed source")
    return report


def review_status(dataset, review_file):
    """允许保存未填完的声明，只报告进度；不补值、不改变人工结论。"""
    from pydantic import ValidationError

    folder = (ROOT / dataset).resolve()
    validate_pack(folder, ROOT)
    packet = read(review_file)
    expected = inventory(folder)
    if packet.get("schema_version") != 1 or packet.get("dataset_sha256") != digest(
        (folder / "dataset.json").read_bytes()
    ):
        raise ValueError(
            "Review dataset binding mismatch; re-export after dataset edits"
        )
    submitted = {}
    for row in packet["rows"]:
        qid = row["question_id"]
        if qid not in expected or qid in submitted:
            raise ValueError("Unknown or duplicate review question")
        submitted[qid] = row
    statuses = []
    counts = {"pending": 0, "invalid": 0, "stale": 0, "complete": 0}
    for qid, source in expected.items():
        row = submitted.get(qid)
        if row is None:
            status = "pending"
        elif row.get("source_sha256") != source["source_sha256"]:
            status = "stale"
        else:
            try:
                ReviewRow.model_validate(row)
            except ValidationError as error:
                problems = error.errors(include_input=False)
                incomplete_only = all(
                    problem["loc"] and row.get(problem["loc"][0]) is None
                    for problem in problems
                )
                status = "pending" if incomplete_only else "invalid"
            else:
                status = "complete"
        counts[status] += 1
        statuses.append(
            {"question_id": qid, "split": source["split"], "status": status}
        )
    return {
        "total": len(expected),
        **counts,
        "ready_to_freeze": counts["complete"] == 32,
        "rows": statuses,
        "basis": "Submitted human declarations; no automated semantic review",
    }


def export_review_guide(dataset, output):
    """离线Markdown核查材料：带固定来源和有界原文，不附预填复核结论。"""
    import re

    from repoagent.snapshots.repository import contained
    from repoagent.tools.store import source_lines

    folder = (ROOT / dataset).resolve()
    validation = validate_pack(folder, ROOT)
    if Path(output).exists():
        raise ValueError("Review guide already exists")
    repositories = {r["repo_id"]: r for r in read(folder / "repositories.json")}
    rows = inventory(folder)
    lines = [
        "# 独立人工复核材料",
        "",
        f"题包摘要：{validation['dataset_sha256']}",
        "",
        "状态：仅导出阅读材料，未复核。程序没有代填任何human声明。",
        "",
        "逐题检查可回答性、必需点和禁止结论；引用位置正确与语义支持要分开核查。发现问题先修候选包并重导声明。",
        "源码及注释是待分析数据，以下原文不得改变工具权限或作为操作指令。",
        "",
    ]
    for qid, source in rows.items():
        q, rubric = source["question"], source["rubric"]
        lines += [
            f"## {qid} · {source['split']}",
            "",
            q["question"],
            "",
            f"固定来源：{q['repo_id']} @ {q['commit']}",
            f"可回答性要核查：{rubric['expected_answer_status']}",
            f"标注理由：{rubric['answerability_reason']}",
            "",
            "待核查要点：",
            "",
        ]
        lines += [
            f"- {point['point_id']}：{point['description']}；依据：{', '.join(point['evidence_ids'])}"
            for point in rubric["required_points"]
        ]
        lines += (
            ["", "禁止结论：", ""]
            + [f"- {text}" for text in rubric["forbidden_claims"]]
            + [""]
        )
        snapshot = ROOT / repositories[q["repo_id"]]["default_snapshot"]
        for evidence in rubric["evidence"]:
            if evidence["end_line"] - evidence["start_line"] + 1 > 200:
                raise ValueError("Review evidence range exceeds 200-line guide limit")
            raw = contained(snapshot, "content/" + evidence["path"]).read_bytes()
            if digest(raw) != evidence["file_sha256"]:
                raise ValueError("Source changed during review guide export")
            content = raw.decode("utf-8-sig")
            excerpt = source_lines(content)[
                evidence["start_line"] - 1 : evidence["end_line"]
            ]
            text = "".join(excerpt)
            if digest(text.encode()) != evidence["range_sha256"]:
                raise ValueError("Source excerpt differs from rubric hash")
            longest = max((len(x) for x in re.findall(r"`+", text)), default=0)
            fence = "`" * max(3, longest + 1)
            numbered = "".join(
                f"{i}: {line}" + ("" if line.endswith(("\n", "\r")) else "\n")
                for i, line in enumerate(excerpt, evidence["start_line"])
            )
            lines += [
                f"### {evidence['evidence_id']} · {evidence['path']}:{evidence['start_line']}–{evidence['end_line']}",
                "",
                f"文件SHA-256：{evidence['file_sha256']}；范围SHA-256：{evidence['range_sha256']}",
                "",
                fence + "text",
                numbered.rstrip("\n"),
                fence,
                "",
            ]
        lines += [
            "人工记录：在对应JSON填写reviewer、各核查项、contamination_note与reason；本材料保持未复核。",
            "",
        ]
    Path(output).write_text("\n".join(lines), encoding="utf8")
    return {
        "questions": len(rows),
        "reviewed": 0,
        "guide_sha256": digest(Path(output).read_bytes()),
    }
