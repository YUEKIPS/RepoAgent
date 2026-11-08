"""Price the model-visible envelope; estimates never replace provider usage."""

import json
import math


def estimate_text(text):
    # Conservative mixed-language heuristic. This is not a model tokenizer.
    ascii_count = sum(ord(char) < 128 for char in text)
    return math.ceil(ascii_count / 3 + (len(text) - ascii_count))


def text_size(value, counter=None):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return (counter or estimate_text)(text)


class RequestMeter:
    def measure(self, prepared, capabilities=None, limits=None, counter=None, identity=None):
        payload = json.loads(prepared.payload)
        messages = payload.get("messages")
        if messages is not None:
            components = {
                "messages": sum(text_size(message, counter) for message in messages),
                "tools": text_size(payload["tools"], counter) if payload.get("tools") else 0,
                "output_schema": text_size(payload["response_format"], counter) if payload.get("response_format") else 0,
                "protocol_overhead": 16 + len(messages) * 12,
            }
        else:
            components = {"simulated_request": text_size(json.loads(prepared.request_json)), "protocol_overhead": 16}
        estimated = sum(components.values())
        advertised = getattr(capabilities, "context_window", None)
        configured = getattr(limits, "context_window_tokens", None)
        if configured and advertised and configured > advertised:
            raise ValueError("Configured window exceeds model capability")
        window = configured or advertised
        output = prepared.max_output
        source = "explicit"
        if output is None:
            output = getattr(capabilities, "max_output_tokens", None)
            source = "capability_max" if output else "unknown"
        headroom = getattr(limits, "headroom_tokens", None)
        if headroom is None:
            headroom = min(65536, window // 16) if window else 0
        ratio = getattr(limits, "pressure_ratio", 0.8)
        threshold = math.floor(min(window * ratio, window - output - headroom)) if window and output else None
        if threshold is not None and threshold <= 0:
            raise ValueError("Invalid output reservation or context headroom")
        byte_limit = getattr(limits, "max_input_estimate", None)
        return {
            "estimated_input_tokens": estimated,
            "estimation_method": "provider-tokenizer-json-envelope-v1" if counter else "mixed-unicode-heuristic-v1",
            "text_tokenizer": identity,
            "exact_text_tokenizer": counter is not None,
            "exact_tokenizer": False, "context_window": window,
            "reserved_output_tokens": output, "output_reservation_source": source,
            "headroom_tokens": headroom, "trigger_tokens": threshold,
            "input_bytes": prepared.input_size, "components": components,
            "pressure": bool((threshold and estimated >= threshold) or (byte_limit and prepared.input_size > byte_limit)),
            "hard_fits": not (window and output and estimated + output > window),
            "window_pressure_available": threshold is not None,
            "request_sha256": prepared.wire_sha256,
        }
