"""Tolerant extraction of JSON from model output.

Models sometimes wrap JSON in Markdown fences or add a sentence before or after it. These
helpers find the first JSON value of the wanted kind instead of failing on the extra text.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)
_DECODER = json.JSONDecoder()


def _candidates(text: str) -> list[str]:
    """Fenced blocks first (most explicit), then the raw text."""
    fenced = [match.group(1) for match in _FENCE_RE.finditer(text)]
    return [*fenced, text]


def _scan(text: str, kinds: tuple[type, ...]) -> Any | None:
    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except ValueError:
        pass
    else:
        if isinstance(value, kinds):
            return value
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = _DECODER.raw_decode(text, index)
        except ValueError:
            continue
        if isinstance(value, kinds):
            return value
    return None


def extract_json(text: str, kinds: tuple[type, ...] = (dict, list)) -> Any | None:
    """Return the first JSON value in ``text`` whose type is one of ``kinds``, else ``None``."""
    if not text:
        return None
    for candidate in _candidates(text):
        value = _scan(candidate, kinds)
        if value is not None:
            return value
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    value = extract_json(text, (dict,))
    return value if isinstance(value, dict) else None


def extract_json_array(text: str) -> list[Any] | None:
    value = extract_json(text, (list,))
    return value if isinstance(value, list) else None
