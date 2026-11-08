"""Language-neutral declarations and source locations, without code execution."""

from typing import Literal

from pydantic import BaseModel, Field

MAX_FILE_BYTES = 1024 * 1024


class Symbol(BaseModel):
    symbol_id: str
    path: str
    kind: str
    name: str
    owner: str | None
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class ParseResult(BaseModel):
    path: str
    content_sha256: str
    parse_status: Literal["parsed", "syntax_error", "unavailable"]
    symbols: list[Symbol]
    warnings: list[str]
