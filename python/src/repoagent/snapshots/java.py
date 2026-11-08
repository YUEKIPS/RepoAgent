"""Java语法结构解析：将UTF-8字节转换为声明、所属类型和可引用行号。

Tree-sitter只分析语法，不编译/执行代码，也不提供完整类型解析或精确调用图。
输入路径由调用方绑定；文件摘要和声明位置标识有不同用途。
"""

from hashlib import sha256

import tree_sitter_java
from tree_sitter import Language, Parser

from repoagent.snapshots.symbols import MAX_FILE_BYTES, ParseResult, Symbol

# 只提取声明节点；方法调用、局部变量等节点仍会遍历，但不作为符号输出。
DECLARATIONS = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
    "method_declaration",
    "constructor_declaration",
    "compact_constructor_declaration",
}


def parse_java(source: bytes, path: str) -> ParseResult:
    """解析source字节并返回声明列表、行号、原始摘要及解析状态。

    参数path只用于来源展示和位置标识，调用方负责快照/路径边界。
    仅支持UTF-8及BOM，超限/编码错误抛ValueError；语法错误返回syntax_error与部分结构。
    使用TreeCursor按源码顺序遍历，不做Java类型检查或调用绑定。
    """
    if len(source) > MAX_FILE_BYTES:
        raise ValueError("Java source exceeds the 1 MiB file limit")
    try:
        # utf-8-sig 会移除开头的 BOM，不改变后续文本的行号。
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Java source must be UTF-8 (BOM is supported)") from error
    # Tree-sitter 的位置按字节计算，名称切片必须使用同一份 UTF-8 字节。
    normalized = text.encode("utf-8")
    parser = Parser(Language(tree_sitter_java.language()))
    # 保留Tree对象直到遍历结束，避免原生节点引用脱离语法树生命周期。
    tree = parser.parse(normalized)
    root = tree.root_node
    symbols = []
    # 使用官方TreeCursor遍历，减少批量节点访问，配合本机验证过的依赖版本。
    # owners记录每层所属类型；不使用Python递归或缓存整批子节点。
    cursor = tree.walk()
    owners = [None]
    finished = False
    while not finished:
        node = cursor.node
        owner = owners[-1]
        child_owner = owner
        if node.type in DECLARATIONS:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = normalized[name_node.start_byte : name_node.end_byte].decode(
                    "utf-8"
                )
                # 同名方法可能重载，因此不能仅以名字作为定义标识。
                identity = f"{path}:{node.type}:{node.start_byte}:{node.end_byte}"
                symbols.append(
                    Symbol(
                        symbol_id=sha256(identity.encode()).hexdigest()[:24],
                        path=path,
                        kind=node.type.removesuffix("_declaration"),
                        name=name,
                        owner=owner,
                        # Tree-sitter 行号从 0 开始，外部引用统一从 1 开始。
                        start_line=node.start_point.row + 1,
                        # end_point 为不包含的结束位置；列为 0 时落在下一行行首，
                        # 因此不能直接把该下一行算入闭区间。
                        end_line=max(
                            node.start_point.row + 1,
                            node.end_point.row + (1 if node.end_point.column else 0),
                        ),
                    )
                )
                # 类型声明更新后代的 owner；方法和构造器不改变所属类型。
                if node.type not in {
                    "method_declaration",
                    "constructor_declaration",
                    "compact_constructor_declaration",
                }:
                    child_owner = f"{owner}.{name}" if owner else name
        if cursor.goto_first_child():
            owners.append(child_owner)
            continue
        # 没有孩子时先找兄弟，找不到则回到父层，保持源码顺序的深度优先遍历。
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                finished = True
                break
            owners.pop()
    # 可以提取到符号不等于整个文件合法，必须单独检查语法错误标记。
    return ParseResult(
        path=path,
        content_sha256=sha256(source).hexdigest(),
        parse_status="syntax_error" if root.has_error else "parsed",
        symbols=symbols,
        warnings=(
            ["Syntax errors detected; symbol coverage is incomplete."]
            if root.has_error
            else []
        ),
    )
