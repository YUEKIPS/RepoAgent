"""Python declarations with lexical owners; never import the analyzed module."""

from hashlib import sha256

import tree_sitter_python
from tree_sitter import Language, Parser

from repoagent.snapshots.symbols import MAX_FILE_BYTES, ParseResult, Symbol


def parse_python(source: bytes, path: str) -> ParseResult:
    if len(source) > MAX_FILE_BYTES:
        raise ValueError("Python source exceeds the 1 MiB file limit")
    normalized = source.decode("utf-8-sig").encode("utf-8")
    tree = Parser(Language(tree_sitter_python.language())).parse(normalized)
    cursor, scopes, finished = tree.walk(), [(None, None)], False
    symbols = []
    while not finished:
        node = cursor.node
        owner, scope_kind = scopes[-1]
        child_scope = (owner, scope_kind)
        if node.type in {"class_definition", "function_definition"}:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = normalized[name_node.start_byte:name_node.end_byte].decode("utf-8")
                kind = "class" if node.type == "class_definition" else (
                    "method" if scope_kind == "class" else "function"
                )
                if node.type == "function_definition" and node.child(0).type == "async":
                    kind = "async_" + kind
                span = node.parent if node.parent.type == "decorated_definition" else node
                identity = f"{path}:{node.type}:{span.start_byte}:{span.end_byte}"
                symbols.append(Symbol(
                    symbol_id=sha256(identity.encode()).hexdigest()[:24],
                    path=path, kind=kind, name=name, owner=owner,
                    start_line=span.start_point.row + 1,
                    end_line=max(span.start_point.row + 1,
                                 span.end_point.row + bool(span.end_point.column)),
                ))
                child_scope = (f"{owner}.{name}" if owner else name,
                               "class" if kind == "class" else "function")
        if cursor.goto_first_child():
            scopes.append(child_scope)
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                finished = True
                break
            scopes.pop()
    return ParseResult(
        path=path, content_sha256=sha256(source).hexdigest(),
        parse_status="syntax_error" if tree.root_node.has_error else "parsed",
        symbols=symbols,
        warnings=["Syntax errors detected; symbol coverage is incomplete."]
        if tree.root_node.has_error else [],
    )
