"""Pure model-facing views of tool results. Raw observations stay in the journal."""

import json
from copy import deepcopy

PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"


def head_tail(text, threshold=8192, head=4096, tail=1024):
    if min(threshold, head, tail) < 0 or head + tail + len(PRUNE_MARKER) > threshold:
        raise ValueError("Invalid head/tail budget")
    if len(text) <= threshold:
        return text
    return text[:head] + PRUNE_MARKER + (text[-tail:] if tail else "")


def external_preview(result, limits):
    """Text budget spans blocks; non-text content and rich-block ordering survive."""
    result = deepcopy(result)
    blocks = result.get("content", [])
    total = sum(len(block.get("text", "")) for block in blocks if block.get("type") == "text")
    structured = result.get("structuredContent")
    # Structured JSON and its compatibility text can be identical, not additive input.
    if structured is not None:
        encoded = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        equivalents = []
        for block in blocks:
            try:
                if block.get("type") == "text" and json.loads(block["text"]) == structured:
                    equivalents.append(block)
            except (ValueError, KeyError, TypeError):
                pass
        if equivalents:
            result.pop("structuredContent")
        elif len(encoded) > limits.prune_threshold_chars:
            blocks = blocks + [{"type": "text", "text": encoded}]
            result["content"] = blocks
            result.pop("structuredContent")
        total = sum(len(block.get("text", "")) for block in blocks if block.get("type") == "text")
    if total <= limits.prune_threshold_chars:
        return result
    start, end = limits.prune_head_chars, total - limits.prune_tail_chars
    output, position, inserted = [], 0, False
    for block in blocks:
        if block.get("type") != "text":
            output.append(block)
            continue
        text = block.get("text", "")
        left = max(0, min(len(text), start - position))
        right = max(0, min(len(text), end - position))
        marker = ""
        if position < end and position + len(text) > start and not inserted:
            marker, inserted = PRUNE_MARKER, True
        visible = text[:left] + marker + text[right:]
        if visible:
            output.append({**block, "text": visible})
        position += len(text)
    result["content"] = output
    result["context_truncated"] = True
    result["original_text_chars"] = total
    return result


def navigation_preview(result, limits):
    result = deepcopy(result)
    if "items" in result:
        keys = {"path", "repo_id", "source_kind", "source_version_id", "snapshot_id", "commit", "start_line", "end_line", "name", "kind", "symbol_id", "match_count", "type", "status"}
        result["items"] = [{k: v for k, v in item.items() if k in keys} for item in result["items"]]
        result["observation_compacted"] = True
    return result


def compact_navigation(result):
    """Group only identical fixed-source identity fields; retain raw results elsewhere.

    Conflicting/partial per-hit identities stay on their hit rather than being inferred.
    repo_id remains on every routed hit, including repositories sharing a snapshot.
    """
    result = deepcopy(result)
    keys = {"source_kind", "source_version_id", "snapshot_id", "commit"}
    groups = {}
    root_repo = result.get("repo_id")
    if root_repo is not None:
        groups[root_repo] = {k: result[k] for k in keys if k in result}
    for item in result.get("items", []):
        repo = item.get("repo_id")
        identity = {k: item[k] for k in keys if k in item}
        if repo is not None and identity and repo not in groups:
            groups[repo] = identity
        elif repo in groups and identity and all(identity.get(k) == v for k, v in groups[repo].items()):
            # Historical model-visible observations may have only a partial root identity.
            groups[repo] = identity | groups[repo]
    # A one-hit group merely moves its identity and adds a wrapper. Keep it inline.
    groups = {repo: identity for repo, identity in groups.items()
              if sum(item.get("repo_id") == repo and bool(identity)
                     and all(item.get(k) == v for k, v in identity.items())
                     for item in result.get("items", [])) > 1}
    if groups:
        result["sources"] = groups
        for item in result.get("items", []):
            identity = groups.get(item.get("repo_id"), {})
            if identity and all(item.get(k) == v for k, v in identity.items()):
                for key in identity:
                    item.pop(key, None)
        if root_repo in groups:
            for key in groups[root_repo]:
                result.pop(key, None)
    return result


def preserve(result, limits):
    return deepcopy(result)
