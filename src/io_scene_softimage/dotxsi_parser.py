# SPDX-License-Identifier: GPL-3.0-or-later
"""Fast structural parser for text dotXSI files.

The parser only looks at the structure of the file (braces, quoted strings and comments) with one
C-speed regular expression scan. Every template keeps the character spans of its own values; the values
are split into tokens only when a converter asks for them, and large numeric arrays are converted with
NumPy in one call. A 30 MB animation file therefore costs a single regex pass plus the arrays that are
actually used.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterator

import numpy as np


class DotXSIError(ValueError):
    """Raised when a dotXSI file cannot be decoded safely."""


@dataclass(frozen=True)
class Header:
    version: str
    encoding: str
    float_bits: int
    raw: str


_HEADER = re.compile(
    rb"^(?:\xef\xbb\xbf)?xsi\s+([0-9]{4})(txt|bin)\s+([0-9]{4})\s*$",
    re.IGNORECASE,
)
_HEADER_TEXT = re.compile(r"xsi\s+([0-9]{4})(txt|bin)\s+([0-9]{4})\s*", re.IGNORECASE)
CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Structural scan: strings (skipped as a whole so braces inside them do not count), comments that start
# a token, and braces. Paths are written with single backslashes and no escapes, so a string ends at
# the next double quote.
_STRUCT = re.compile(
    r'"[^"]*"'
    r"|(?<![^\s{};,])(?://|\#)[^\r\n]*"
    r"|/\*.*?\*/"
    r"|[{}]",
    re.DOTALL,
)
# A template header at the end of the text before "{": an identifier, optionally followed by a name.
# The two must be separated by whitespace only (values are always separated by ',' or ';').
_HEADER_TAIL = re.compile(
    r'(?:(?<=[\s,;{}])|^)(?P<template>[A-Za-z_][A-Za-z0-9_]*)'
    r'(?:[ \t\r\n]+(?P<name>"[^"]*"|[^\s,;{}"]+))?[ \t\r\n]*\Z'
)
_VALUE_TOKEN = re.compile(r'"([^"]*)"|([^\s,;"]+)')
_SEPARATORS = str.maketrans({",": " ", ";": " "})
_ONLY_SEPARATORS = re.compile(r"[\s,;]*\Z")


class QuotedStr(str):
    """A token that was written between double quotes; never converted to a number."""

    __slots__ = ()


class Element:
    """One template instance. `values` holds its own values (children excluded)."""

    __slots__ = ("template", "name", "children", "pos", "_source", "_spans", "_tokens", "_values")

    def __init__(self, template: str, name: str, source: "_Source", pos: int):
        self.template = template
        self.name = name
        self.children: list[Element] = []
        self.pos = pos
        self._source = source
        self._spans: list[tuple[int, int]] = []
        self._tokens: list[str] | None = None
        self._values: list[object] | None = None

    def __repr__(self) -> str:
        return f"<Element {self.template} {self.name!r} line {self.line}>"

    @property
    def line(self) -> int:
        return self._source.line_of(self.pos)

    @property
    def tokens(self) -> list[str]:
        """Raw value tokens (quoted tokens are QuotedStr without their quotes)."""
        if self._tokens is None:
            text = self._source.text
            out: list[str] = []
            for start, end in self._spans:
                chunk = text[start:end]
                if '"' in chunk:
                    for quoted, atom in _VALUE_TOKEN.findall(chunk):
                        out.append(atom if atom else QuotedStr(quoted))
                else:
                    out.extend(chunk.translate(_SEPARATORS).split())
            self._tokens = out
        return self._tokens

    @property
    def values(self) -> list[object]:
        """Tokens converted to int, float or str."""
        if self._values is None:
            self._values = [_convert_token(token) for token in self.tokens]
        return self._values

    def child(self, *templates: str) -> "Element | None":
        wanted = set(templates)
        return next((item for item in self.children if item.template in wanted), None)

    def children_of(self, *templates: str) -> list["Element"]:
        wanted = set(templates)
        return [item for item in self.children if item.template in wanted]

    def descendants(self, *templates: str) -> Iterator["Element"]:
        wanted = set(templates)
        for child in self.children:
            if child.template in wanted:
                yield child
            yield from child.descendants(*templates)

    # ------------------------------------------------------------------ numeric access
    def floats(self, start: int, count: int, what: str = "value") -> np.ndarray:
        """`count` float64 values starting at token `start`."""
        tokens = self.tokens
        if start < 0 or count < 0 or start + count > len(tokens):
            raise DotXSIError(
                f"{what} in {self.template} at line {self.line} is truncated: needs {count} "
                f"values from position {start}, only {max(0, len(tokens) - start)} remain."
            )
        chunk = tokens[start:start + count]
        try:
            array = np.array(chunk, dtype=np.float64)
        except ValueError:
            array = np.array([_float_token(token, self, what) for token in chunk], dtype=np.float64)
        if count and not np.isfinite(array).all():
            raise DotXSIError(f"{what} in {self.template} at line {self.line} is not finite.")
        return array

    def ints(self, start: int, count: int, what: str = "index") -> np.ndarray:
        """`count` integer values (int64) starting at token `start`."""
        array = self.floats(start, count, what)
        as_int = array.astype(np.int64)
        if count and not np.array_equal(as_int, array):
            raise DotXSIError(f"{what} in {self.template} at line {self.line} is not an integer.")
        return as_int

    def number(self, index: int, what: str = "value") -> float:
        tokens = self.tokens
        if index >= len(tokens):
            raise DotXSIError(f"{what} in {self.template} at line {self.line} is missing.")
        return _float_token(tokens[index], self, what)

    def integer(self, index: int, what: str = "value") -> int:
        value = self.number(index, what)
        if not float(value).is_integer():
            raise DotXSIError(
                f"{what} in {self.template} at line {self.line} is not an integer: {value!r}."
            )
        return int(value)


def _convert_token(token: str) -> object:
    if isinstance(token, QuotedStr):
        return str(token)
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    if "d" in token or "D" in token:
        try:
            return float(token.replace("D", "E").replace("d", "e"))
        except ValueError:
            pass
    return token


def _float_token(token: str, element: Element, what: str) -> float:
    if not isinstance(token, QuotedStr):
        try:
            value = float(token)
        except ValueError:
            try:
                value = float(token.replace("D", "E").replace("d", "e"))
            except ValueError:
                value = None
        if value is not None and value == value and abs(value) != float("inf"):
            return value
    raise DotXSIError(
        f"{what} in {element.template} at line {element.line} is not a finite number: {token!r}."
    )


class _Source:
    __slots__ = ("text", "line_offset", "_newlines")

    def __init__(self, text: str, line_offset: int):
        self.text = text
        self.line_offset = line_offset
        self._newlines: np.ndarray | None = None

    def line_of(self, pos: int) -> int:
        if self._newlines is None:
            self._newlines = np.array(
                [m.start() for m in re.finditer("\n", self.text)], dtype=np.int64
            )
        return int(np.searchsorted(self._newlines, pos, side="right")) + 1 + self.line_offset


def parse(text: str, line_offset: int = 0) -> list[Element]:
    """Parse the body of a dotXSI file (everything after the header line)."""
    return list(iter_elements(text, line_offset))


def iter_elements(text: str, line_offset: int = 0) -> Iterator[Element]:
    source = _Source(text, line_offset)
    stack: list[Element] = []
    segment = 0
    for match in _STRUCT.finditer(text):
        char = text[match.start()]
        if char == '"':
            continue
        start = match.start()
        if char != "{" and char != "}":
            # A comment ends the current value segment.
            _add_span(stack, source, segment, start)
            segment = match.end()
            continue
        if char == "{":
            tail_start = max(segment, start - 1024)
            header = _HEADER_TAIL.search(text, tail_start, start)
            if header is None:
                raise DotXSIError(
                    f"Expected a template name before '{{' at line {source.line_of(start)}."
                )
            _add_span(stack, source, segment, header.start())
            name = header.group("name") or ""
            if name.startswith('"'):
                name = name[1:-1]
            element = Element(header.group("template"), name, source, header.start())
            if stack:
                stack[-1].children.append(element)
            stack.append(element)
            segment = match.end()
            continue
        # "}"
        if not stack:
            raise DotXSIError(f"Unexpected '}}' at line {source.line_of(start)}.")
        _add_span(stack, source, segment, start)
        element = stack.pop()
        segment = match.end()
        if not stack:
            yield element
    if stack:
        raise DotXSIError(
            f"Unclosed {stack[0].template} block starting at line {stack[0].line}."
        )
    _add_span(stack, source, segment, len(text))


def _add_span(stack: list[Element], source: _Source, start: int, end: int) -> None:
    if start >= end:
        return
    if stack:
        stack[-1]._spans.append((start, end))
        return
    if not _ONLY_SEPARATORS.match(source.text, start, end):
        fragment = source.text[start:end].strip()[:32]
        raise DotXSIError(
            f"Expected a template at line {source.line_of(start)}; found {fragment!r}."
        )


# ---------------------------------------------------------------------- header / file access
def read_header(path) -> Header:
    with open(path, "rb") as stream:
        first = stream.readline(256).rstrip(b"\r\n")
    return header_from_line(first)


def header_from_line(first: bytes) -> Header:
    match = _HEADER.match(first)
    if not match:
        if first.startswith(CFB_SIGNATURE):
            raise DotXSIError("This is a native Softimage scene; import it as .scn or .emdl.")
        shown = first[:64].decode("ascii", "replace")
        raise DotXSIError(f"Not a recognized dotXSI header: {shown!r}")
    encoding = match.group(2).decode("ascii").lower()
    if encoding == "bin":
        raise DotXSIError(
            "Binary dotXSI is not supported. Re-export the file from Softimage as ASCII text."
        )
    return Header(
        version=match.group(1).decode("ascii"),
        encoding=encoding,
        float_bits=int(match.group(3)),
        raw=first.decode("ascii", "replace"),
    )


def split_text(text: str) -> tuple[Header, str]:
    """Header and body of dotXSI text held in memory."""
    first, separator, body = text.lstrip("﻿").partition("\n")
    match = _HEADER_TEXT.fullmatch(first.rstrip("\r"))
    if not match:
        raise DotXSIError(f"Not a recognized dotXSI header: {first[:64]!r}")
    if match.group(2).lower() == "bin":
        raise DotXSIError(
            "Binary dotXSI is not supported. Re-export the file from Softimage as ASCII text."
        )
    if not separator:
        raise DotXSIError("The dotXSI text contains a header but no body.")
    return Header(match.group(1), "txt", int(match.group(3)), first), body


def decode_body(data: bytes, warnings: list[str]) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        warnings.append("The file is not UTF-8; decoded it as Windows-1252.")
        return data.decode("cp1252", "replace")
