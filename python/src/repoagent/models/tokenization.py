"""Pinned provider vocabulary, without transformers or remote Python execution."""

import hashlib
from functools import lru_cache
from importlib.resources import files
from urllib.parse import urlsplit

TOKENIZER_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"
TOKENIZER_SOURCE = "https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip"


@lru_cache(maxsize=1)
def _deepseek_v4():
    from tokenizers import Tokenizer
    data = files("repoagent.models").joinpath("assets/deepseek-v4-tokenizer.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("Tokenizer data integrity failure")
    tokenizer = Tokenizer.from_str(data.decode("utf-8"))
    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids)
    return count


def counter_for(config):
    if (config and config.provider == "deepseek" and urlsplit(config.base_url).hostname == "api.deepseek.com"
        and config.model in {"deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}):
        return _deepseek_v4(), {"id": "deepseek-v4-official-20261107", "sha256": TOKENIZER_SHA256,
                                "source": TOKENIZER_SOURCE, "protocol_estimated": True}
    return None, {"id": "mixed-unicode-heuristic-v1", "protocol_estimated": True}
