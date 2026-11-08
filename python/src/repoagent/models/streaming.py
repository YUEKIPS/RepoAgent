"""Bounded streaming assembly; extract only a top-level answer JSON string."""

import json


class DraftDecoder:
    def __init__(self):
        self.depth = 0
        self.in_string = self.escape = False
        self.key = ""
        self.value_key = None
        self.buffer = ""
        self.decoded = ""
        self.answer_buffer = ""
        self.capturing = self.is_key = False
        self.expect_key = False
        self.size = 0

    def feed(self, fragment):
        self.size += len(fragment)
        if self.size > 2_000_000:
            raise ValueError("stream_capacity")
        for char in fragment:
            if self.in_string:
                if self.escape:
                    if self.capturing or self.is_key:
                        self.buffer += char
                    if self.capturing:
                        self.answer_buffer += char
                    self.escape = False
                elif char == "\\":
                    if self.capturing or self.is_key:
                        self.buffer += char
                    if self.capturing:
                        self.answer_buffer += char
                    self.escape = True
                elif char == '"':
                    if self.is_key:
                        self.value_key = json.loads('"' + self.buffer + '"')
                        self.expect_key = False
                    self.in_string = False
                    self.is_key = False
                    if self.capturing:
                        self.capturing = False
                elif self.capturing or self.is_key:
                    self.buffer += char
                    if self.capturing:
                        self.answer_buffer += char
            elif char == '"':
                self.in_string = True
                self.is_key = self.depth == 1 and self.expect_key
                self.capturing = self.depth == 1 and not self.is_key and self.value_key == "answer"
                if self.is_key or self.capturing:
                    self.buffer = ""
            elif char in "{[":
                self.depth += 1
                self.expect_key = self.depth == 1
            elif char in "}]":
                self.depth -= 1
            elif char == "," and self.depth == 1:
                self.expect_key = True
                self.value_key = None
        if self.answer_buffer:
            try:
                value = json.loads('"' + self.answer_buffer + '"')
                if value and 0xD800 <= ord(value[-1]) <= 0xDBFF:
                    return ""
                delta = value[len(self.decoded):]
                self.decoded = value
                return delta
            except (ValueError, UnicodeError):
                pass
        return ""
