"""Tolerant JSON extraction used by the planner and critic."""

from __future__ import annotations

import pytest

from research_assistant.agents.parsing import (
    extract_json,
    extract_json_array,
    extract_json_object,
)


def test_plain_json_array() -> None:
    assert extract_json_array('[{"question": "a"}]') == [{"question": "a"}]


def test_fenced_json_with_prose() -> None:
    text = 'Here is the plan:\n```json\n{"approved": false, "issues": ["x"]}\n```\nThanks!'
    assert extract_json_object(text) == {"approved": False, "issues": ["x"]}


def test_unlabelled_fence() -> None:
    assert extract_json_array("```\n[1, 2]\n```") == [1, 2]


def test_json_embedded_in_prose() -> None:
    text = 'Sure. {"approved": true} Hope that helps.'
    assert extract_json_object(text) == {"approved": True}


def test_skips_invalid_candidates_and_wrong_kinds() -> None:
    text = 'Not json {oops} then [1, 2] then {"a": 1}'
    assert extract_json_object(text) == {"a": 1}
    assert extract_json_array(text) == [1, 2]


def test_first_value_of_any_kind() -> None:
    assert extract_json('prefix [1] {"a": 1}') == [1]


@pytest.mark.parametrize("text", ["", "no json here", "{broken", "[1, 2"])
def test_returns_none_when_nothing_usable(text: str) -> None:
    assert extract_json(text) is None
    assert extract_json_object(text) is None
    assert extract_json_array(text) is None
