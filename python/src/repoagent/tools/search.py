"""Shared literal matching, ranking and output contracts for fixed-source search.

Backends decide which files are readable; these helpers never fetch or execute code.
"""

from repoagent.snapshots.repository import safe_relative


def search_spec(queries, kind="text", path=None, output_mode="content"):
    queries = [queries] if isinstance(queries, str) else queries
    if (
        not isinstance(queries, list)
        or not 1 <= len(queries) <= 3
        or any(not isinstance(q, str) or not q.strip() or len(q) > 512 for q in queries)
    ):
        raise ValueError("Expected 1 to 3 nonblank queries up to 512 characters")
    if kind not in {"text", "path", "symbol"}:
        raise ValueError("Invalid search kind")
    if output_mode not in {"content", "files_with_matches", "count"}:
        raise ValueError("Invalid search output mode")
    if path is not None:
        if not isinstance(path, str):
            raise ValueError("Invalid search path")
        # Accept a conventional directory suffix, but not repeated separators.
        path = path[:-1] if len(path) > 1 and path.endswith("/") else path
        path = safe_relative(path, allow_root=True) or None
    return dict(
        queries=[q.strip() for q in queries],
        kind=kind,
        path=path,
        output_mode=output_mode,
    )


def in_scope(file, path):
    return path is None or file == path or file.startswith(path + "/")


def source_lines(text: str) -> list[str]:
    """Only LF splits source lines; preserve CRLF and Unicode separators."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def symbol_match(row, term):
    qualified = f"{row['owner']}.{row['name']}" if row.get("owner") else row["name"]
    names = [row["name"].casefold(), qualified.casefold()]
    return 3 if term in names else int(any(term in name for name in names))


def file_hits(path, text, queries):
    terms = [q.casefold() for q in queries]
    for number, original in enumerate(source_lines(text), 1):
        line = original.rstrip("\r\n")
        scores = [
            int(term in line.casefold()) + int(q in line and q != term)
            for q, term in zip(queries, terms, strict=True)
        ]
        if any(scores):
            yield dict(
                path=path,
                start_line=number,
                end_line=number,
                snippet=line[:160],
                snippet_truncated=len(line) > 160,
                score=sum(scores),
                _query_scores=scores,
            )


def declaration_hits(symbols, queries):
    for symbol in symbols:
        scores = [symbol_match(symbol, q.casefold()) for q in queries]
        if any(scores):
            yield symbol | dict(score=sum(scores), _query_scores=scores)


def path_hit(path, queries):
    scores = [int(q.casefold() in path.casefold()) for q in queries]
    return (
        dict(path=path, start_line=1, score=sum(scores), _query_scores=scores)
        if any(scores)
        else None
    )


def rank_hits(hits, query_count):
    def key(hit, score):
        return (
            -score,
            hit["path"],
            hit["start_line"],
            hit.get("name", ""),
            hit.get("symbol_id", ""),
        )

    hits.sort(key=lambda h: key(h, h["score"]))
    if query_count > 1:
        queues = [
            iter(
                sorted(
                    (h for h in hits if h["_query_scores"][i]),
                    key=lambda h: key(h, h["_query_scores"][i]),
                )
            )
            for i in range(query_count)
        ]
        merged, seen = [], set()
        while True:
            advanced = False
            for queue in queues:
                for hit in queue:
                    identity = (
                        hit["path"],
                        hit["start_line"],
                        hit.get("end_line"),
                        hit.get("symbol_id"),
                    )
                    if identity not in seen:
                        seen.add(identity)
                        merged.append(hit)
                        advanced = True
                        break
            if not advanced:
                break
        hits = merged
    return [{k: v for k, v in hit.items() if k != "_query_scores"} for hit in hits]


def search_output(hits, spec, scope):
    """Counts are unique matching lines/files/declarations, never occurrences."""
    counts = {}
    for hit in hits:
        counts[hit["path"]] = counts.get(hit["path"], 0) + 1
    metadata = dict(
        kind=spec["kind"],
        output_mode=spec["output_mode"],
        path_filter=spec["path"],
        match_count=len(hits),
        file_count=len(counts),
        count_scope=scope,
        match_unit={"text": "line", "path": "file", "symbol": "declaration"}[
            spec["kind"]
        ],
        note="无匹配不代表功能不存在。",
    )
    if spec["output_mode"] == "content":
        return hits, metadata
    rows = [
        {"path": path}
        | ({"match_count": count} if spec["output_mode"] == "count" else {})
        for path, count in counts.items()
    ]
    return rows, metadata


def enclosing_types(hits, symbols):
    types = {}
    for symbol in symbols:
        if symbol["kind"] in {
            "class",
            "interface",
            "enum",
            "record",
            "annotation_type",
        }:
            types.setdefault(symbol["path"], []).append(symbol)
    for hit in hits:
        containing = [
            s
            for s in types.get(hit["path"], [])
            if s["start_line"] <= hit["start_line"]
            and hit.get("end_line", hit["start_line"]) <= s["end_line"]
        ]
        if containing:
            parent = min(
                containing,
                key=lambda s: (
                    s["end_line"] - s["start_line"],
                    s["start_line"],
                    s["symbol_id"],
                ),
            )
            hit.update(
                enclosing_type_start_line=parent["start_line"],
                enclosing_type_end_line=parent["end_line"],
            )
