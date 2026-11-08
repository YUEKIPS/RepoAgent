"""候选题集离线校验：检查版本、配额、快照和代码引用的一致性。

问题和rubrics分离；public_input只导出允许进入任务的数据。
位置/摘要通过不证明评分要点的语义，也不产生模型正确率。
"""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repoagent.snapshots.repository import READABLE, contained, verify_snapshot
from repoagent.tools.store import source_lines

SPLITS = ("debug", "validation", "unanswerable-debug")
EXPECTED = {
    "debug": {"location": 5, "flow": 4, "exception": 1},
    "validation": {"location": 5, "flow": 6, "exception": 4, "unanswerable": 5},
    "unanswerable-debug": {"unanswerable": 2},
}
COMMIT = r"^[0-9a-f]{40}$"
SHA256 = r"^[0-9a-f]{64}$"


# 题集专用严格模型基类；禁止额外字段，避免评分材料悄悄混入问题结构。
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# 公开问题及版本/题型元数据；送模型前再经public_input白名单裁剪。
class Question(StrictModel):
    question_id: str = Field(min_length=1)
    repo_id: str = Field(min_length=1)
    commit: str = Field(pattern=COMMIT)
    category: Literal["location", "flow", "exception", "unanswerable"]
    question: str = Field(min_length=1, max_length=4000)
    log_text: str | None = Field(default=None, max_length=12000)


# 评分依据的文件/范围双摘要与行号；不同于执行器实际发送的Evidence模型。
class Evidence(StrictModel):
    evidence_id: str = Field(min_length=1)
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    file_sha256: str = Field(pattern=SHA256)
    range_sha256: str = Field(pattern=SHA256)


# 人工评分所需的一条要点及支撑证据ID列表。
class Point(StrictModel):
    point_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str]


# 与公开问题分离的评分要点、禁止结论、无答案理由及人工复核状态。
class Rubric(StrictModel):
    question_id: str
    expected_answer_status: Literal["answered", "insufficient_evidence"]
    topic_group: str = Field(min_length=1)
    answerability_reason: str = Field(min_length=1)
    review_status: Literal["needs_human_review", "reviewed"]
    required_points: list[Point] = Field(min_length=1)
    forbidden_claims: list[str] = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)


# 固定仓库登记：commit、许可证及摘要、默认快照和解析器版本。
class Repository(StrictModel):
    repo_id: str
    url: str
    commit: str = Field(pattern=COMMIT)
    license: Literal["Apache-2.0"]
    license_path: str
    license_sha256: str = Field(pattern=SHA256)
    default_snapshot: str
    inventory: str
    versions: dict[str, str]


def sha256(data: bytes) -> str:
    """返回data字节摘要；文件和选中行范围分别计算，避免只验证其中一层。"""
    return hashlib.sha256(data).hexdigest()


def jsonl(path: Path, model: type[BaseModel]) -> list:
    """逐行读取path并用model校验，返回模型列表；拒绝空行而不是悄悄跳过记录。"""
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ValueError(f"Blank JSONL row: {path.name}:{number}")
        rows.append(model.model_validate_json(line))
    return rows


def unique(rows: list, field: str) -> dict:
    """将rows按field建索引并返回字典；发现重复ID立即抛ValueError。"""
    result = {}
    for row in rows:
        key = getattr(row, field)
        if key in result:
            raise ValueError(f"Duplicate {field}: {key}")
        result[key] = row
    return result


def public_input(question: Question) -> dict:
    """从question白名单导出请求可用字段，返回字典。

    不输出category/rubrics/答案；调用方仍需绑定当前验证过的snapshot_id。
    """
    return question.model_dump(
        include={"question_id", "repo_id", "commit", "question", "log_text"}
    )


def check_evidence(snapshot: Path, evidence: Evidence) -> None:
    """核对snapshot内evidence的文件摘要、行范围和范围摘要，成功返回None。

    位置/原文一致不代表引用支持评分语义；非法路径、旧摘要或越界抛错误。
    """
    data = contained(snapshot, "content/" + evidence.path).read_bytes()
    if sha256(data) != evidence.file_sha256:
        raise ValueError(f"Evidence file hash mismatch: {evidence.path}")
    lines = source_lines(data.decode("utf-8-sig"))
    if not 1 <= evidence.start_line <= evidence.end_line <= len(lines):
        raise ValueError(f"Evidence line range invalid: {evidence.path}")
    selected = "".join(lines[evidence.start_line - 1 : evidence.end_line]).encode()
    if sha256(selected) != evidence.range_sha256:
        raise ValueError(f"Evidence range hash mismatch: {evidence.path}")


def check_rubric(question: Question, rubric: Rubric) -> None:
    """核对question与rubric的可答状态和ID关联，要求可答要点有已登记证据。"""
    expected = (
        "insufficient_evidence" if question.category == "unanswerable" else "answered"
    )
    if (
        rubric.question_id != question.question_id
        or rubric.expected_answer_status != expected
    ):
        raise ValueError(f"Rubric answerability mismatch: {question.question_id}")
    evidence = unique(rubric.evidence, "evidence_id")
    unique(rubric.required_points, "point_id")
    for point in rubric.required_points:
        if not set(point.evidence_ids) <= evidence.keys():
            raise ValueError(f"Unknown evidence ID: {question.question_id}")
        if question.category != "unanswerable" and not point.evidence_ids:
            raise ValueError(f"Answerable point lacks evidence: {question.question_id}")


def check_distribution(questions: dict[str, list[Question]]) -> dict:
    """检查各split题型配额、跨组唯一ID及两仓库覆盖，返回分布统计字典。"""
    seen = set()
    distribution = {}
    for split in SPLITS:
        actual = Counter(q.category for q in questions[split])
        if actual != EXPECTED[split]:
            raise ValueError(f"Question distribution mismatch: {split}")
        ids = unique(questions[split], "question_id")
        if seen.intersection(ids):
            raise ValueError("Question IDs overlap across splits")
        seen.update(ids)
        distribution[split] = {
            "count": len(ids),
            "categories": dict(actual),
            "repositories": dict(Counter(q.repo_id for q in questions[split])),
        }
    repositories = {q.repo_id for rows in questions.values() for q in rows}
    if len(repositories) != 2:
        raise ValueError("Expected exactly two repositories")
    for split in ("debug", "validation"):
        if {q.repo_id for q in questions[split]} != repositories:
            raise ValueError(f"Both repositories must appear: {split}")
    return distribution


def validate_pack(dataset: Path, project: Path) -> dict:
    """离线校验dataset及project内引用快照，返回可复现检查报告。

    依次检查包元数据/文件摘要、仓库与覆盖、问题配额、评分关联和引用位置。
    不调用模型、不自动评分；frozen状态必须已标记全部人工复核。
    """
    meta = json.loads((dataset / "dataset.json").read_text(encoding="utf-8"))
    if (
        meta["schema_version"] != 1
        or meta["status"] not in {"candidate", "frozen"}
        or meta["splits"] != list(SPLITS)
        or meta["formal_question_count"] != 30
        or meta["extra_debug_count"] != 2
    ):
        raise ValueError("Invalid dataset metadata")
    registry = json.loads((dataset / "repositories.json").read_text(encoding="utf-8"))
    repos = unique([Repository.model_validate(row) for row in registry], "repo_id")
    expected_files = {"repositories.json"}
    expected_files.update(repo.inventory for repo in repos.values())
    expected_files.update(
        f"{kind}/{split}.jsonl" for kind in ("questions", "rubrics") for split in SPLITS
    )
    if set(meta["files_sha256"]) != expected_files:
        raise ValueError("Dataset file inventory mismatch")
    # 先核对数据包全部文件摘要，再读题集与快照；修改后不能沿用原版本成绩。
    for name, expected_hash in meta["files_sha256"].items():
        if (
            not re.fullmatch(SHA256, expected_hash)
            or sha256(contained(dataset, name).read_bytes()) != expected_hash
        ):
            raise ValueError(f"Dataset hash mismatch: {name}")
    snapshots = {}
    coverage = {}
    for key, repo in repos.items():
        snapshot = contained(project, repo.default_snapshot)
        manifest = verify_snapshot(snapshot)
        if manifest.commit != repo.commit or manifest.versions != repo.versions:
            raise ValueError(f"Snapshot version mismatch: {key}")
        inventory = json.loads(
            contained(dataset, repo.inventory).read_text(encoding="utf-8")
        )
        expected_inventory = {row["path"]: row["sha256"] for row in inventory}
        actual_inventory = {
            f.path: f.sha256 for f in manifest.files if f.status in READABLE
        }
        if (
            len(inventory) != len(expected_inventory)
            or expected_inventory != actual_inventory
        ):
            raise ValueError(f"Snapshot content inventory mismatch: {key}")
        snapshots[key] = snapshot
        coverage[key] = {
            "commit": manifest.commit,
            "snapshot_id": manifest.snapshot_id,
            "state": manifest.state,
            "coverage": manifest.coverage,
        }
    questions = {
        split: jsonl(dataset / "questions" / f"{split}.jsonl", Question)
        for split in SPLITS
    }
    distribution = check_distribution(questions)
    if {q.repo_id for rows in questions.values() for q in rows} != set(repos):
        raise ValueError("Question repository does not match registry")
    references = reviewed = 0
    topics = {}
    for split in SPLITS:
        rubrics = unique(
            jsonl(dataset / "rubrics" / f"{split}.jsonl", Rubric), "question_id"
        )
        if set(rubrics) != {q.question_id for q in questions[split]}:
            raise ValueError(f"Missing or extra rubric: {split}")
        topics[split] = set()
        for question in questions[split]:
            if question.commit != repos[question.repo_id].commit:
                raise ValueError(f"Question commit mismatch: {question.question_id}")
            rubric = rubrics[question.question_id]
            check_rubric(question, rubric)
            topics[split].add((question.repo_id, rubric.topic_group))
            reviewed += rubric.review_status == "reviewed"
            for evidence in rubric.evidence:
                check_evidence(snapshots[question.repo_id], evidence)
                references += 1
    # 显式主题分组用于避免明显交叉，但不能证明语义近重复或预训练污染不存在。
    if (topics["debug"] | topics["unanswerable-debug"]) & topics["validation"]:
        raise ValueError("Explicit topic group overlaps between debug and validation")
    if meta["status"] == "frozen" and reviewed != 32:
        raise ValueError("Cannot freeze unreviewed rubrics")
    return {
        "dataset_version": meta["dataset_version"],
        "dataset_sha256": sha256((dataset / "dataset.json").read_bytes()),
        "status": meta["status"],
        "distribution": distribution,
        "snapshots": coverage,
        "reference_checks_passed": references,
        "human_reviewed_rubrics": reviewed,
        "validator_sha256": sha256(Path(__file__).read_bytes()),
        "model_calls": 0,
        "model_quality_evaluated": False,
        "limitations": [
            "引用位置/摘要通过不代表引用支持评分要点；语义与无答案边界待人工复核。",
            "题型/显式主题ID检查不能排除语义近重复或预训练污染。",
        ],
    }
