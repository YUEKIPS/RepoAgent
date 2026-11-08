"""One dispatch table for snapshot and on-demand source indexing."""

import importlib.metadata
from dataclasses import dataclass
from functools import partial
from pathlib import PurePosixPath

from repoagent.snapshots.declarations import parse_declarations
from repoagent.snapshots.frontend import parse_frontend
from repoagent.snapshots.java import parse_java
from repoagent.snapshots.python import parse_python


@dataclass(frozen=True)
class LanguageSpec:
    extensions: tuple[str, ...]
    parser: object
    packages: tuple[str, ...]


def _header(source, path):
    cpp = parse_declarations(source, path, "cpp")
    if cpp.parse_status == "parsed":
        cpp.warnings.append("Header indexed with C++ syntax; actual compile language is not inferred.")
        return cpp
    c = parse_declarations(source, path, "c")
    if c.parse_status == "parsed":
        c.warnings.append("Header indexed with C syntax; actual compile language is not inferred.")
        return c
    return cpp if cpp.parse_status == "unavailable" else c


SPECS = {
    "java": LanguageSpec((".java",), parse_java, ("tree-sitter-java",)),
    "python": LanguageSpec((".py", ".pyi"), parse_python, ("tree-sitter-python",)),
    "typescript": LanguageSpec((".ts", ".mts", ".cts"), partial(parse_frontend, language="typescript"), ("tree-sitter-typescript",)),
    "tsx": LanguageSpec((".tsx",), partial(parse_frontend, language="tsx"), ("tree-sitter-typescript",)),
    "javascript": LanguageSpec((".js", ".jsx", ".mjs", ".cjs"), partial(parse_frontend, language="javascript"), ("tree-sitter-javascript",)),
    "go": LanguageSpec((".go",), partial(parse_declarations, language="go"), ("tree-sitter-go",)),
    "csharp": LanguageSpec((".cs",), partial(parse_declarations, language="csharp"), ("tree-sitter-c-sharp",)),
    "c": LanguageSpec((".c",), partial(parse_declarations, language="c"), ("tree-sitter-c",)),
    "cpp": LanguageSpec((".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"), partial(parse_declarations, language="cpp"), ("tree-sitter-cpp",)),
    "c_family": LanguageSpec((".h",), _header, ("tree-sitter-c", "tree-sitter-cpp")),
    "rust": LanguageSpec((".rs",), partial(parse_declarations, language="rust"), ("tree-sitter-rust",)),
    "php": LanguageSpec((".php", ".phtml"), partial(parse_declarations, language="php"), ("tree-sitter-php",)),
}
LANGUAGES = {suffix: name for name, spec in SPECS.items() for suffix in spec.extensions}
PARSER_PACKAGES = tuple(dict.fromkeys(["tree-sitter", *(p for spec in SPECS.values() for p in spec.packages)]))
INDEX_POLICY = "mainstream-index-v1"
TEXT_LANGUAGES = {".sql": "sql", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
                  ".ps1": "powershell", ".psm1": "powershell", ".toml": "toml", ".tf": "hcl", ".hcl": "hcl"}
SOURCE_EXTENSIONS = frozenset(LANGUAGES) | {".sql", ".sh", ".bash", ".zsh", ".ps1", ".psm1"}


def language_for(path: str):
    suffix = PurePosixPath(path).suffix
    if suffix == ".C":
        return "cpp"
    return LANGUAGES.get(suffix.lower()) or TEXT_LANGUAGES.get(suffix.lower())


def parser_versions():
    versions = {}
    for name in PARSER_PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return versions | {"index_policy": INDEX_POLICY}


def parse_source(source: bytes, path: str, language: str):
    return SPECS[language].parser(source, path)
