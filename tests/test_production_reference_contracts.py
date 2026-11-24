from __future__ import annotations

import json

from pydantic import ValidationError
import pytest

from tests.e2e_reference._contracts import Conversation, Observation, Runtime, accepted_indexes


@pytest.mark.parametrize("statuses,expected", [([200] * 4, [0, 1, 2, 3]), ([409, 200], [1])])
def test_accepted_indexes(statuses: list[int], expected: list[int]) -> None:
    assert accepted_indexes(statuses) == expected


@pytest.mark.parametrize("statuses", [[], [409, 409], [200, 500], [401]])
def test_contention_rejects_invalid_outcomes(statuses: list[int]) -> None:
    with pytest.raises(AssertionError):
        accepted_indexes(statuses)


def test_complete_conversation() -> None:
    conversation = Conversation.model_validate(
        {
            "messages": [
                {"type": "human", "content": "a"},
                {"type": "ai", "content": "[fake model] observed 1 human turn(s)"},
                {"type": "human", "content": "b"},
                {"type": "ai", "content": "[fake model] observed 2 human turn(s)"},
            ]
        }
    )
    conversation.check_turns(["b", "a"])


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"type": "human", "content": "a"}],
        [{"type": "human", "content": "b"}, {"type": "ai", "content": "wrong"}],
        [{"type": "human", "content": "a"}, {"type": "ai", "content": "wrong"}],
        [{"type": "human", "content": "a"}, {"type": "human", "content": "a"}],
    ],
)
def test_torn_or_lost_conversation(messages: list[dict[str, str]]) -> None:
    conversation = Conversation.model_validate({"messages": messages})
    with pytest.raises(AssertionError):
        conversation.check_turns(["a"])


@pytest.mark.parametrize("extra", [{"hostname": "private"}, {"key": "private"}])
def test_evidence_forbids_unlisted_fields(extra: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Observation.model_validate({"tag": "auth", "seconds": 1, **extra})


@pytest.mark.parametrize("tag", ["https://private", "AccountKey=private", "a" * 33])
def test_evidence_forbids_arbitrary_tags(tag: str) -> None:
    with pytest.raises(ValidationError):
        Observation(tag=tag, seconds=0)


def test_evidence_round_trip() -> None:
    observed = Observation(tag="contention", statuses=(200, 409), count=1, seconds=0.25)
    assert json.loads(observed.model_dump_json()) == {
        "tag": "contention",
        "statuses": [200, 409],
        "count": 1,
        "seconds": 0.25,
    }


def test_runtime_rejects_non_version_text() -> None:
    with pytest.raises(ValidationError):
        Runtime.model_validate(dict.fromkeys(Runtime.model_fields, "private-host"))
