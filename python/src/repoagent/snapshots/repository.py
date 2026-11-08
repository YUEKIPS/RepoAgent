"""固定Git版本快照：从commit对象读取文件，建立清单、覆盖和SQLite索引。

构建不checkout、不编译或运行目标仓库；查询前复验内容及索引摘要。
路径校验、策略排除与处理失败分别记录，避免把未分析内容当作已覆盖。
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repoagent.snapshots.languages import (
    LANGUAGES,
    SOURCE_EXTENSIONS,
    TEXT_LANGUAGES,
    language_for,
    parse_source,
    parser_versions,
)
from repoagent.snapshots.symbols import MAX_FILE_BYTES

EXCLUDED_DIRS = {
    ".git",
    "target",
    "build",
    "node_modules",
    ".venv",
    ".conda",
    "dist",
    "coverage",
    ".next",
    "vendor",
    "obj", ".terraform", ".gradle", ".dart_tool",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", "venv", "site-packages", ".hypothesis",
}
EXTENSIONS = set(LANGUAGES) | set(TEXT_LANGUAGES) | {
    ".java",
    ".py",
    ".pyi",
    ".xml",
    ".yml",
    ".yaml",
    ".properties",
    ".md",
    ".ts",
    ".js",
    ".json",
    ".vue",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".scss",
    ".mjs",
    ".cjs",
}
CONFIG_NAMES = {
    ".env",
    ".env.example",
    ".env.development",
    ".env.production",
    ".env.test",
    ".env.staging",
    "pyproject.toml", "setup.cfg", "requirements.txt",
    "tox.ini", "pytest.ini", "Pipfile",
    "Dockerfile", "Containerfile", "Makefile", "CMakeLists.txt", "go.mod", "go.sum",
    "Cargo.lock", "Gemfile", "Rakefile", "composer.lock",
}
READABLE = {"parsed", "text", "text_only"}


def supported_file(path: str) -> bool:
    item = PurePosixPath(path)
    return (item.suffix.lower() in EXTENSIONS or item.name in CONFIG_NAMES
            or item.name.startswith(("Dockerfile.", "Containerfile."))
            or item.suffix.lower() in {".cmake", ".csproj", ".fsproj", ".props", ".targets", ".gradle"}
            or item.name.startswith("requirements-") and item.suffix.lower() == ".txt"
            or "requirements" in item.parts[:-1] and item.suffix.lower() == ".txt")


def excluded_directory(path: str) -> bool:
    parts = PurePosixPath(path).parts[:-1]
    return bool(EXCLUDED_DIRS.intersection(parts)) or any(
        left == "bin" and right.casefold() in {"debug", "release"}
        for left, right in zip(parts, parts[1:])
    )


class Limits(BaseModel):
    """可配置的原型规模限制，不代表性能测量值。"""

    model_config = ConfigDict(extra="forbid")
    max_file_bytes: int = Field(default=MAX_FILE_BYTES, gt=0, le=MAX_FILE_BYTES)
    max_java_files: int = Field(default=1000, gt=0)
    max_source_files: int = Field(default=2000, gt=0)
    max_readable_files: int = Field(default=5000, gt=0)
    max_total_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_tree_entries: int = Field(default=10000, gt=0)


# 单文件清单项：Git对象ID、原始大小/摘要、处理状态和跳过或降级原因。
class FileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    # Git对象ID与下方文件SHA-256用途不同，不能混作同一种摘要。
    blob: str
    language: str | None = None
    size_bytes: int
    # parsed可结构检索，text/text_only可文本读取；skipped/excluded保留覆盖原因。
    status: Literal["parsed", "text", "text_only", "skipped", "excluded"]
    reason: str = ""
    sha256: str | None = None


# 快照级清单：固定版本、规模限制、文件覆盖、索引摘要与解析器版本。
class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2]
    @property
    def source_kind(self):
        return "local_snapshot"

    # 保存时由其余字段计算；状态/清单改变也会改变该身份。
    snapshot_id: str = ""
    commit: str
    source: str
    source_mode: Literal["git_objects"] = "git_objects"
    # 先INDEXING，成功READY/PARTIAL，普通失败FAILED；只允许完成态查询。
    state: Literal["INDEXING", "READY", "PARTIAL", "FAILED"] = "INDEXING"
    limits: Limits
    versions: dict[str, str]
    files: list[FileRecord] = Field(default_factory=list)
    coverage: dict[str, int] = Field(default_factory=dict)
    # 索引也属于固定快照内容，查询前核对，不能只校验源码文件。
    index_sha256: str | None = None
    index_bytes: int = 0
    error: str | None = None


def digest(data: bytes) -> str:
    """返回data原始字节的完整SHA-256十六进制摘要，不做文本归一化。"""
    return hashlib.sha256(data).hexdigest()


def safe_relative(path: str, *, allow_root: bool = False) -> str:
    """校验并返回POSIX相对路径字符串。

    参数path不得含绝对根、盘符、设备名、反斜杠或穿越段；allow_root允许空目录。
    这是字符串级检查，文件系统实际边界由contained另行校验。
    """
    if allow_root and path in {"", "."}:
        return ""
    parts = path.split("/")
    devices = {"CON", "PRN", "AUX", "NUL"} | {
        f"{kind}{n}" for kind in ("COM", "LPT") for n in range(1, 10)
    }
    if (
        not path
        or PurePosixPath(path).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(char in path for char in '\\:*?"<>|')
        or any(ord(char) < 32 for char in path)
        or any(part.endswith((".", " ")) for part in parts)
        or any(part.split(".")[0].upper() in devices for part in parts)
    ):
        raise ValueError("Unsafe relative path")
    return path


def contained(root: Path, relative: str) -> Path:
    """返回root内relative对应的已检查路径，拒绝符号链接/junction及解析后的越界。

    不靠字符串前缀判断包含关系；发现异常路径时抛ValueError。
    """
    relative = safe_relative(relative, allow_root=True)
    current = root
    for part in ("", *relative.split("/")):
        if part:
            current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError("Links and junctions are not allowed in snapshots")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes snapshot root")
    return current


def git_bytes(repo: Path, *args: str) -> bytes:
    # 不用 shell，不应用 Git replace 对象，不让当前进程的仓库环境覆盖 -C。
    """在repo中以参数列表调用受限Git子进程并返回stdout字节。

    args为独立参数，不交给shell；非零退出、超时等转换为输入错误，不执行目标源码。
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        }
    }
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo), *args],
            capture_output=True,
            timeout=30,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Git read failed or timed out") from error
    if result.returncode:
        # 不回显配置/远端等潜在敏感错误；操作种类足以定位本阶段问题。
        raise ValueError(f"Git operation failed: {args[0]}")
    return result.stdout


def manifest_identity(manifest: Manifest) -> str:
    """对清单除snapshot_id自身外的字段稳定序列化并求摘要，返回快照身份。"""
    payload = manifest.model_dump(exclude={"snapshot_id"})
    return digest(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode())


def save_manifest(root: Path, manifest: Manifest) -> None:
    """根据manifest.files重算覆盖和身份后写到root；状态变化也会改变清单标识。"""
    manifest.coverage = {"scanned": len(manifest.files)} | {
        status: sum(file.status == status for file in manifest.files)
        for status in ("parsed", "text", "text_only", "skipped", "excluded")
    }
    manifest.snapshot_id = manifest_identity(manifest)
    (root / "manifest.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )


def build_snapshot(
    repo: Path, output: Path, ref: str = "HEAD", limits: Limits | None = None
) -> Manifest:
    """从repo的ref构建新output快照并返回Manifest。

    limits控制扫描/文件/总量；先落INDEXING，再筛选Git对象、解析和建立SQLite索引。
    普通失败记录FAILED并抛出；输出不能覆盖已有目录；不编译或运行目标Java。
    """
    limits = limits or Limits()
    commit = (
        git_bytes(
            repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
        )
        .decode()
        .strip()
    )
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("Expected a full Git commit")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Snapshot output already exists; choose a new directory")
    output.mkdir(parents=True)
    manifest = Manifest(
        schema_version=2,
        commit=commit,
        source=str(repo.resolve()),
        limits=limits,
        versions=parser_versions(),
    )
    # 先落盘INDEXING；进程被中断/原生扩展崩溃时也不会留下可查询的半成品。
    save_manifest(output, manifest)
    texts = {}
    symbols = []
    try:
        # -l 获取 blob 大小，先判断限制；-z 可正确区分带空格的路径。
        entries = git_bytes(
            repo, "ls-tree", "-r", "-l", "-z", "--full-tree", commit
        ).split(b"\0")
        entries = [entry for entry in entries if entry]
        if len(entries) > limits.max_tree_entries:
            raise ValueError("Git tree exceeds entry limit")
        java_count = source_count = readable_count = total_bytes = 0
        seen = set()
        # 处理的是commit树条目，不遍历工作区；文件状态分别记录策略排除和读取/解析缺口。
        for entry in entries:
            header, raw_path = entry.split(b"\t", 1)
            mode, kind, blob, size = header.split()
            path = raw_path.decode("utf-8")
            file = FileRecord(
                path=path,
                blob=blob.decode(),
                size_bytes=0 if size == b"-" else int(size),
                status="excluded",
            )
            manifest.files.append(file)
            try:
                safe_relative(path)
            except ValueError:
                file.status, file.reason = "skipped", "unsafe_path"
                continue
            if path.casefold() in seen:
                raise ValueError("Case-insensitive path collision")
            seen.add(path.casefold())
            suffix = PurePosixPath(path).suffix.lower()
            # Git链接和子模块不物化到content，避免导入后绕过根目录边界。
            if mode == b"120000" or kind != b"blob":
                file.reason = "link_or_submodule"
                continue
            if excluded_directory(path) or (
                not supported_file(path)
            ):
                file.reason = "excluded_by_policy"
                continue
            java_count += suffix == ".java"
            source_count += suffix in SOURCE_EXTENSIONS
            readable_count += 1
            file.language = language_for(path)
            total_bytes += file.size_bytes
            if (
                java_count > limits.max_java_files
                or source_count > limits.max_source_files
                or readable_count > limits.max_readable_files
                or total_bytes > limits.max_total_bytes
            ):
                raise ValueError(
                    "Repository exceeds source/file count or total byte limit"
                )
            if file.size_bytes > limits.max_file_bytes:
                file.status, file.reason = "skipped", "file_too_large"
                continue
            content = git_bytes(repo, "cat-file", "blob", file.blob)
            if len(content) != file.size_bytes:
                raise ValueError("Git blob size mismatch")
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                file.status, file.reason = "skipped", "unsupported_encoding"
                continue
            if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
                file.status, file.reason = "skipped", "binary_content"
                continue
            file.sha256 = digest(content)
            file.status = "text"
            if suffix in LANGUAGES:
                parsed = parse_source(content, path, file.language)
                # 语法错误保留可读文本但不写入部分符号，避免把不完整结构当精确索引。
                file.status = (
                    "parsed" if parsed.parse_status == "parsed" else "text_only"
                )
                if file.status == "parsed":
                    symbols.extend(parsed.symbols)
                else:
                    file.reason = "syntax_error" if parsed.parse_status == "syntax_error" else "parser_unavailable"
            elif suffix in SOURCE_EXTENSIONS or suffix == ".vue":
                file.status, file.reason = "text_only", "structure_not_supported"
            target = contained(output / "content", path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            texts[path] = text
        if not texts:
            raise ValueError("No readable repository files")
        # 使用参数化 SQL；索引与源文件同属于此快照，不依赖原仓库工作区。
        with closing(sqlite3.connect(output / "index.sqlite3")) as db, db:
            db.executescript(
                "CREATE TABLE files(path TEXT PRIMARY KEY, text TEXT, status TEXT);"
                "CREATE TABLE symbols(symbol_id TEXT PRIMARY KEY, path TEXT, kind TEXT,"
                " name TEXT, owner TEXT, start_line INTEGER, end_line INTEGER, language TEXT);"
                "CREATE INDEX symbols_name ON symbols(name);"
            )
            db.executemany(
                "INSERT INTO files VALUES(?,?,?)",
                [
                    (file.path, texts[file.path], file.status)
                    for file in manifest.files
                    if file.path in texts
                ],
            )
            db.executemany(
                "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        s.symbol_id,
                        s.path,
                        s.kind,
                        s.name,
                        s.owner,
                        s.start_line,
                        s.end_line,
                        language_for(s.path),
                    )
                    for s in symbols
                ],
            )
        index = output / "index.sqlite3"
        manifest.index_bytes = index.stat().st_size
        manifest.index_sha256 = digest(index.read_bytes())
        manifest.state = (
            "PARTIAL"
            if not any(file.status == "parsed" for file in manifest.files)
            or any(file.status in {"skipped", "text_only"} for file in manifest.files)
            else "READY"
        )
    except (OSError, ValueError, sqlite3.Error) as error:
        manifest.state = "FAILED"
        manifest.error = str(error)
        save_manifest(output, manifest)
        raise ValueError(f"Snapshot failed: {error}") from error
    save_manifest(output, manifest)
    return manifest


def verify_snapshot(root: Path) -> Manifest:
    """复验root的清单、可读内容及索引，返回可查询Manifest。

    检查文件清单、大小和摘要而非只检查目录存在；变更或越界立即拒绝。
    摘要一致只证明内部一致性，不能单独证明仓库来源可信。
    """
    root = root.absolute()
    path = contained(root, "manifest.json")
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Manifest exceeds size limit")
    manifest = Manifest.model_validate_json(path.read_bytes())
    if manifest.snapshot_id != manifest_identity(manifest):
        raise ValueError("Manifest checksum mismatch")
    if manifest.state not in {"READY", "PARTIAL"}:
        raise ValueError(f"Snapshot not queryable: {manifest.state}")
    # 完整性同时核对已知文件和目录实际清单，新增文件也属于快照变化。
    expected = set()
    for file in manifest.files:
        if file.status not in READABLE:
            continue
        safe_relative(file.path)
        expected.add(file.path)
        target = contained(root, "content/" + file.path)
        if (
            target.stat().st_size != file.size_bytes
            or file.size_bytes > manifest.limits.max_file_bytes
        ):
            raise ValueError("Snapshot content size changed")
        if digest(target.read_bytes()) != file.sha256:
            raise ValueError("Snapshot content checksum mismatch")
    actual = set()
    for directory, directories, filenames in os.walk(
        contained(root, "content"), followlinks=False
    ):
        for name in directories + filenames:
            item = Path(directory) / name
            contained(root, item.relative_to(root).as_posix())
        for name in filenames:
            actual.add(
                (Path(directory) / name).relative_to(root / "content").as_posix()
            )
    if actual != expected:
        raise ValueError("Snapshot file inventory changed")
    index = contained(root, "index.sqlite3")
    if (
        index.stat().st_size != manifest.index_bytes
        or digest(index.read_bytes()) != manifest.index_sha256
    ):
        raise ValueError("Snapshot index checksum mismatch")
    return manifest
