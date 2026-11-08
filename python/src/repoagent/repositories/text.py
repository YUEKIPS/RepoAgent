"""受控文本解释：原字节不变，只有明确的编码策略能生成可引用文字。"""

import codecs
from dataclasses import dataclass
from hashlib import sha256

from repoagent.repositories.github import GitHubError
from repoagent.snapshots.java import MAX_FILE_BYTES
from repoagent.snapshots.repository import safe_relative

DECODER_VERSION = "source-text-v2-auto"
SUPPORTED_DECODERS = frozenset({DECODER_VERSION})
TEXT_ERRORS = frozenset(
    {
        "unsupported_text_encoding",
        "text_encoding_required",
        "text_encoding_conflict",
        "unsupported_text_characters",
        "decoded_text_limit",
    }
)
ENCODINGS = frozenset({"auto", "utf-8", "utf-16", "gbk", "gb18030"})


def profile(value=None):
    if value is None:
        value = {"default": "auto", "overrides": {}}
    if not isinstance(value, dict) or set(value) != {"default", "overrides"}:
        raise GitHubError("invalid_text_profile")
    overrides = value["overrides"]
    if (
        value["default"] != "auto"
        or not isinstance(overrides, dict)
        or len(overrides) > 128
    ):
        raise GitHubError("invalid_text_profile")
    clean = {}
    for path, encoding in overrides.items():
        if (
            not isinstance(path, str)
            or not isinstance(encoding, str)
            or encoding not in ENCODINGS
        ):
            raise GitHubError("invalid_text_profile")
        safe_relative(path)
        clean[path] = encoding
    return {"default": "auto", "overrides": dict(sorted(clean.items()))}


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str

    @property
    def utf8(self):
        return self.text.encode("utf-8")

    @property
    def digest(self):
        return sha256(self.utf8).hexdigest()


def _checked_text(text, encoding):
    if any(ord(c) < 32 and c not in "\t\r\n" for c in text):
        raise GitHubError("unsupported_text_characters")
    result = DecodedText(text, encoding)
    if len(result.utf8) > MAX_FILE_BYTES:
        raise GitHubError("decoded_text_limit")
    return result


def _fallback_text(raw):
    # 中文源码的有限兼容候选；相同文本不要求用户区分原编辑器标签。
    # 不使用latin-1或replace/ignore保证“成功”，也不重新下载/调用模型。
    candidates = {}
    for encoding in ("gb18030", "gbk"):
        try:
            text = raw.decode(encoding, errors="strict")
            if text.encode(encoding, errors="strict") != raw:
                continue
        except UnicodeError:
            continue
        try:
            decoded = _checked_text(text, encoding)
        except GitHubError as error:
            if error.code == "unsupported_text_characters":
                continue
            raise
        candidates.setdefault(text, decoded)
    if not candidates:
        raise GitHubError("text_encoding_required")
    if len(candidates) != 1:
        raise GitHubError("text_encoding_conflict")
    return next(iter(candidates.values()))


def decode_text(raw, encoding="auto", *, decoder=DECODER_VERSION):
    if len(raw) > MAX_FILE_BYTES:
        raise GitHubError("decoded_text_limit")
    if encoding not in ENCODINGS:
        raise GitHubError("invalid_text_profile")
    if decoder not in SUPPORTED_DECODERS:
        raise GitHubError("unsupported_decoder_version")
    bom = None
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise GitHubError("unsupported_text_encoding")
    if raw.startswith(codecs.BOM_UTF8):
        bom = "utf-8-sig"
    elif raw.startswith(codecs.BOM_UTF16_LE):
        bom = "utf-16-le"
    elif raw.startswith(codecs.BOM_UTF16_BE):
        bom = "utf-16-be"
    expected = "utf-16" if bom and bom.startswith("utf-16") else "utf-8"
    if bom and encoding not in {"auto", expected}:
        raise GitHubError("text_encoding_conflict")
    if encoding == "utf-16" and not (bom and bom.startswith("utf-16")):
        raise GitHubError("text_encoding_required")
    selected = bom or ("utf-8" if encoding == "auto" else encoding)
    data = raw[2:] if selected.startswith("utf-16") else raw
    try:
        text = data.decode(selected, errors="strict")
    except UnicodeError:
        if encoding == "auto" and not bom:
            return _fallback_text(raw)
        code = "unsupported_text_encoding"
        raise GitHubError(code) from None
    return _checked_text(text, selected)
