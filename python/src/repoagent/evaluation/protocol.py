"""冻结实验配置；版本摘要固定后才能正式调度，不产生模型请求。"""

import subprocess
from pathlib import Path

from repoagent.evaluation.batch import ROOT, digest, read, save


def code_revision():
    commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        [
            "git",
            "-C",
            str(ROOT),
            "status",
            "--porcelain",
            "--",
            "python/src/repoagent",
            "config",
            "environment.yml",
            "python/pyproject.toml",
        ],
        text=True,
    )
    if dirty.strip():
        raise ValueError(
            "Commit execution code/configuration before sealing formal experiment"
        )
    return commit


def seal_experiment(config_file, output):
    from repoagent.evaluation.batch import preflight

    config = read(config_file)
    if config.get("stage") != "formal":
        raise ValueError("Only formal configurations can be sealed")
    if Path(output).exists():
        raise ValueError("Refuse to overwrite experiment seal")
    _, identity = preflight(config, sealing=True)
    save(
        output,
        {
            "schema_version": 1,
            "identity_sha256": identity["identity_sha256"],
            "identity": identity,
            "project_commit": code_revision(),
        },
    )
    return {
        "seal_sha256": digest(Path(output).read_bytes()),
        "project_commit": code_revision(),
        "planned_runs": 120,
    }


def verify_seal(c, identity):
    if not c.frozen_manifest_path or not c.frozen_manifest_sha256:
        raise ValueError("Formal experiment seal is required")
    path = ROOT / c.frozen_manifest_path
    if digest(path.read_bytes()) != c.frozen_manifest_sha256:
        raise ValueError("Experiment seal hash mismatch")
    seal = read(path)
    if (
        seal["identity_sha256"] != identity["identity_sha256"]
        or seal["identity"] != identity
        or seal["project_commit"] != code_revision()
    ):
        raise ValueError("Experiment identity differs from frozen seal")
