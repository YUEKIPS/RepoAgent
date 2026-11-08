"""Static declaration adapters. Syntax and lexical scopes are not type binding."""

import importlib
from hashlib import sha256

from tree_sitter import Language, Parser

from repoagent.snapshots.symbols import MAX_FILE_BYTES, ParseResult, Symbol

GRAMMARS = {
    "go": ("tree_sitter_go", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "php": ("tree_sitter_php", "language_php"),
}
RULES = {
    "go": {"function_declaration": "function", "method_declaration": "method",
           "method_elem": "method", "type_spec": "type", "type_alias": "type_alias"},
    "csharp": {"namespace_declaration": "namespace", "file_scoped_namespace_declaration": "namespace",
               "class_declaration": "class", "struct_declaration": "struct", "interface_declaration": "interface",
               "record_declaration": "record", "enum_declaration": "enum", "delegate_declaration": "delegate",
               "method_declaration": "method", "constructor_declaration": "constructor",
               "destructor_declaration": "destructor", "property_declaration": "property",
               "local_function_statement": "function"},
    "c": {"struct_specifier": "struct", "union_specifier": "union", "enum_specifier": "enum"},
    "cpp": {"namespace_definition": "namespace", "class_specifier": "class", "struct_specifier": "struct",
            "union_specifier": "union", "enum_specifier": "enum", "alias_declaration": "type_alias"},
    "rust": {"mod_item": "module", "struct_item": "struct", "enum_item": "enum", "trait_item": "interface",
             "type_item": "type_alias", "function_item": "function", "function_signature_item": "method"},
    "php": {"namespace_definition": "namespace", "class_declaration": "class", "interface_declaration": "interface",
            "trait_declaration": "trait", "enum_declaration": "enum", "function_definition": "function",
            "method_declaration": "method"},
}
CONTAINERS = {"namespace", "class", "struct", "union", "enum", "record", "interface", "trait", "module"}
CALLABLES = {"function", "method", "constructor", "destructor", "async_function", "async_method"}


def _text(raw, node):
    return raw[node.start_byte:node.end_byte].decode("utf-8") if node is not None else ""


def _join(owner, name):
    return f"{owner}.{name}" if owner else name


def _type_name(raw, node):
    while node is not None:
        if node.type in {"type_identifier", "identifier", "scoped_type_identifier"}:
            return _text(raw, node)
        child = node.child_by_field_name("type")
        if child is None:
            child = next(iter(node.named_children), None)
        node = child
    return ""


def _declarator(node):
    while node is not None:
        if node.type == "function_declarator":
            inner = node.child_by_field_name("declarator")
            # (*callback)(...) declares a variable, not a function definition.
            if inner is not None and inner.type == "parenthesized_declarator":
                return None
            return inner
        node = node.child_by_field_name("declarator")
    return None


def _span(node, language):
    first = node
    if node.parent is not None and node.parent.type == "template_declaration":
        return node.parent
    if language == "rust":
        previous = first.prev_named_sibling
        while previous is not None and previous.type == "attribute_item":
            first, previous = previous, previous.prev_named_sibling
    return first


def parse_declarations(source: bytes, path: str, language: str) -> ParseResult:
    if len(source) > MAX_FILE_BYTES:
        raise ValueError("Source exceeds the 1 MiB file limit")
    raw = source.decode("utf-8-sig").encode("utf-8")
    module_name, factory = GRAMMARS[language]
    try:
        grammar = getattr(importlib.import_module(module_name), factory)()
        parser = Parser(Language(grammar))
    except (ImportError, AttributeError, ValueError, OSError):
        return ParseResult(path=path, content_sha256=sha256(source).hexdigest(), parse_status="unavailable",
                           symbols=[], warnings=["Parser unavailable; source remains readable as text."])
    tree = parser.parse(raw)
    cursor, scopes, finished = tree.walk(), [(None, None)], False
    symbols, known_types = [], set()
    while not finished:
        node = cursor.node
        owner, scope_kind = scopes[-1]
        child_scope = (owner, scope_kind)
        kind = RULES[language].get(node.type)
        name_node = node.child_by_field_name("name") if kind else None
        candidates = [(name_node, kind)] if name_node is not None else []
        if language in {"c", "cpp"}:
            if node.type in {"function_definition", "declaration", "field_declaration"}:
                candidates = [(name, "method" if scope_kind in CONTAINERS - {"namespace", "module"} else "function")
                              for dec in node.children_by_field_name("declarator") if (name := _declarator(dec)) is not None]
            elif node.type == "type_definition":
                alias = node.child_by_field_name("declarator")
                while alias is not None and alias.child_by_field_name("declarator") is not None:
                    alias = alias.child_by_field_name("declarator")
                if alias is not None and alias.type in {"identifier", "type_identifier"}:
                    candidates = [(alias, "type_alias")]
        if language == "rust" and node.type == "impl_item":
            target = _type_name(raw, node.child_by_field_name("type"))
            if target:
                child_scope = (_join(owner, target.replace("::", ".")), "class")
        for name_node, kind in candidates:
            name = _text(raw, name_node)
            symbol_owner = owner
            if language == "go":
                if node.type == "type_spec":
                    typ = node.child_by_field_name("type")
                    kind = {"struct_type": "struct", "interface_type": "interface"}.get(typ.type if typ else "", "type_alias")
                if node.type == "method_declaration":
                    receiver = node.child_by_field_name("receiver")
                    parameter = next(iter(receiver.named_children), None) if receiver else None
                    symbol_owner = _type_name(raw, parameter.child_by_field_name("type")) if parameter else None
            if language in {"c", "cpp"} and "::" in name:
                qualifier, name = name.rsplit("::", 1)
                symbol_owner = qualifier.removeprefix("::").replace("::", ".")
                if not _text(raw, name_node).startswith("::"):
                    symbol_owner = _join(owner, symbol_owner)
                if symbol_owner in known_types:
                    kind = "method"
            if language == "rust" and kind in {"function", "method"}:
                kind = "method" if scope_kind in {"class", "interface"} else kind
                modifiers = next((c for c in node.named_children if c.type == "function_modifiers"), None)
                if modifiers is not None and "async" in _text(raw, modifiers).split():
                    kind = "async_" + kind
            unbraced_namespace = node.type == "file_scoped_namespace_declaration" or (
                language == "php" and kind == "namespace" and node.child_by_field_name("body") is None)
            if unbraced_namespace:
                symbol_owner = None
                scopes[-1] = (name.replace("\\", "."), "namespace")
            first = _span(node, language)
            end = first if first.type == "template_declaration" else node
            identity = f"{path}:{node.type}:{name_node.start_byte}:{first.start_byte}:{end.end_byte}"
            symbols.append(Symbol(symbol_id=sha256(identity.encode()).hexdigest()[:24], path=path,
                kind=kind, name=name, owner=symbol_owner, start_line=first.start_point.row+1,
                end_line=max(first.start_point.row+1, end.end_point.row+bool(end.end_point.column))))
            if kind in CONTAINERS or kind in CALLABLES:
                child_scope = (_join(symbol_owner, name.replace("\\", ".")), kind)
                if kind in CONTAINERS - {"namespace", "module"}:
                    known_types.add(child_scope[0])
        if cursor.goto_first_child():
            scopes.append(child_scope)
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                finished = True
                break
            scopes.pop()
    return ParseResult(path=path, content_sha256=sha256(source).hexdigest(), symbols=symbols,
                       parse_status="syntax_error" if tree.root_node.has_error else "parsed",
                       warnings=["Syntax errors detected; symbol coverage is incomplete."] if tree.root_node.has_error else [])
