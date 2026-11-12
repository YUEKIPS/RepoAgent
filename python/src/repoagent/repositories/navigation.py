"""Bounded, domain-neutral catalog hints; navigation never proves code facts."""
import json
import re
from pathlib import PurePosixPath

from repoagent.repositories.sources import GitHubSource


def catalog_hints(sources, question, max_bytes=4096):
    text = question.casefold()
    terms = set(re.findall(r"[a-z_$][a-z0-9_$]{2,}", text)) - {"repository", "commit", "fixed", "source", "tree", "analyze", "only", "the", "and", "with", "what", "does", "how", "why", "where"}
    remotes = [(name, source) for name, source in sorted(sources.items()) if isinstance(source, GitHubSource)]
    result = []
    budget = max_bytes // max(1, len(remotes))
    for name, source in remotes:
        rows, roots = [], set()
        for entry in source.store.entries(source.version):
            if entry["policy"] != "allowed":
                continue
            path = PurePosixPath(entry["path"])
            roots.add(path.parts[0])
            score = 100 * (entry["path"].casefold() in text) + sum(4 * (term in path.stem.casefold()) + (term in entry["path"].casefold()) for term in terms)
            rows.append((-score, len(path.parts), entry["path"]))
        hint = {"repo_id": name, "catalog_only": True, "cached_files": source.descriptor().coverage["cached"],
                "root_entries": sorted(roots)[:12], "paths": []}
        per_root = {}
        for negative_score, depth, candidate in sorted(rows):
            group = PurePosixPath(candidate).parent.as_posix()
            if negative_score == 0 and per_root.get(group, 0) >= 2:
                continue
            hint["paths"].append(candidate)
            if len(json.dumps(hint, ensure_ascii=False).encode()) > budget:
                hint["paths"].pop()
                break
            per_root[group] = per_root.get(group, 0) + 1
            if len(hint["paths"]) >= 16:
                break
        result.append(hint)
    while len(json.dumps(result, ensure_ascii=False).encode()) > max_bytes:
        largest = max(result, key=lambda item: len(json.dumps(item)))
        if largest["paths"]:
            largest["paths"].pop()
        elif largest["root_entries"]:
            largest["root_entries"].pop()
        else:
            return []
    return result
