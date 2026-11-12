"""Bounded JSON representation recovery; never complete or rewrite an answer."""
import json
import re

MAX_RECOVERY_CHARS = 2_000_000
_FENCE = re.compile(r"\s*```(?:json)?\s*\n(.*)\n```\s*", re.DOTALL)


def decode_response_json(content, *, recover_controls=False):
    wrapped = _FENCE.fullmatch(content)
    raw = wrapped.group(1) if wrapped else content

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise json.JSONDecodeError("Duplicate object key", raw, 0)
            result[key] = value
        return result

    def reject_constant(_value):
        raise json.JSONDecodeError("Non-finite JSON number", raw, 0)

    def load(value):
        return json.loads(value, object_pairs_hook=unique_pairs, parse_constant=reject_constant)

    try:
        return load(raw), False
    except json.JSONDecodeError as error:
        if (not recover_controls or len(raw) > MAX_RECOVERY_CHARS
                or not error.msg.startswith("Invalid control character")
                or error.pos >= len(raw) or raw[error.pos] not in "\n\r\t"):
            raise
        # Escape only literal LF/CR/TAB *inside* JSON strings. Existing escapes
        # remain unchanged; truncated strings/objects and all other errors fail.
        output = []
        inside = escaped = changed = False
        for char in raw:
            if escaped:
                output.append(char)
                escaped = False
            elif char == "\\" and inside:
                output.append(char)
                escaped = True
            elif char == '"':
                inside = not inside
                output.append(char)
            elif inside and ord(char) < 32:
                replacement = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(char)
                if replacement is None:
                    raise error
                output.append(replacement)
                changed = True
            else:
                output.append(char)
        if inside or escaped or not changed:
            raise error
        return load("".join(output)), True


def syntax_diagnostic(error):
    """Never include source text, user keys, property names or exception repr."""
    if error.msg.startswith("Invalid control character"):
        kind = "unescaped_string_control"
    elif error.msg == "Duplicate object key":
        kind = "duplicate_object_key"
    elif error.msg == "Non-finite JSON number":
        kind = "nonfinite_number"
    else:
        kind = "invalid_syntax"
    return f"json_syntax:{kind}:line={error.lineno}:column={error.colno}"
