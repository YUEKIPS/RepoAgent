"""TS/JS声明提取；只分析语法，不运行源码，不提供类型绑定或调用图。"""

from hashlib import sha256

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Parser

from repoagent.snapshots.java import MAX_FILE_BYTES, JavaParseResult, JavaSymbol

LANGUAGES = {".ts": "typescript", ".js": "javascript"}
DECLARATIONS = {
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type_alias",
    "enum_declaration": "enum",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "method_definition": "method",
    "method_signature": "method",
    "abstract_method_signature": "method",
    "variable_declarator": "variable",
}


def parse_frontend(source: bytes, path: str, language: str) -> JavaParseResult:
    if len(source) > MAX_FILE_BYTES:
        raise ValueError("Source exceeds the 1 MiB file limit")
    normalized = source.decode("utf-8-sig").encode("utf-8")
    if language == "typescript":
        grammar = tree_sitter_typescript.language_typescript()
    elif language == "javascript":
        grammar = tree_sitter_javascript.language()
    else:
        raise ValueError("Unsupported frontend grammar")
    tree = Parser(Language(grammar)).parse(normalized)
    symbols = []
    cursor, owners, finished = tree.walk(), [None], False
    while not finished:
        node, owner = cursor.node, owners[-1]
        child_owner = owner
        kind = DECLARATIONS.get(node.type)
        name_node = node.child_by_field_name("name") if kind else None
        # 解构、计算属性不猜名称；保留原文，可通过文本检索核查。
        if name_node and name_node.type in {
            "identifier",
            "type_identifier",
            "property_identifier",
        }:
            name = normalized[name_node.start_byte : name_node.end_byte].decode("utf-8")
            value = node.child_by_field_name("value")
            if (
                kind == "variable"
                and value
                and value.type
                in {
                    "arrow_function",
                    "function_expression",
                    "generator_function",
                }
            ):
                kind = "function"
            identity = f"{path}:{node.type}:{node.start_byte}:{node.end_byte}"
            symbols.append(
                JavaSymbol(
                    symbol_id=sha256(identity.encode()).hexdigest()[:24],
                    path=path,
                    kind=kind,
                    name=name,
                    owner=owner,
                    start_line=node.start_point.row + 1,
                    end_line=max(
                        node.start_point.row + 1,
                        node.end_point.row + bool(node.end_point.column),
                    ),
                )
            )
            if kind in {"class", "interface", "enum"}:
                child_owner = f"{owner}.{name}" if owner else name
        if cursor.goto_first_child():
            owners.append(child_owner)
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                finished = True
                break
            owners.pop()
    return JavaParseResult(
        path=path,
        content_sha256=sha256(source).hexdigest(),
        parse_status="syntax_error" if tree.root_node.has_error else "parsed",
        symbols=symbols,
        warnings=["Syntax errors detected; symbol coverage is incomplete."]
        if tree.root_node.has_error
        else [],
    )
