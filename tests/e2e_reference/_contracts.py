"""Value-only assertions and allow-listed evidence for the optional production reference."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    type: Literal["human", "ai"]
    content: str


class Conversation(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    messages: tuple[Message, ...] = ()

    def check_turns(self, expected: Sequence[str]) -> None:
        human = [message.content for message in self.messages if message.type == "human"]
        answers = [message.content for message in self.messages if message.type == "ai"]
        assert Counter(human) == Counter(expected), "persisted human turns differ"
        assert len(answers) == len(expected), "missing or torn assistant turns"
        assert set(answers) == {
            f"[fake model] observed {n} human turn(s)" for n in range(1, len(expected) + 1)
        }, "persisted assistant sequence differs"


class StateResponse(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    values: Conversation


class InvokeResponse(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    output: Conversation


class Runtime(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    python: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    adapter: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    functions: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    langgraph: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    langchain_core: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    blob: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    identity: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    tag: str = Field(pattern=r"^[a-z_]{1,32}$")
    statuses: tuple[int, ...] = ()
    count: int = Field(default=0, ge=0)
    seconds: float = Field(ge=0)


def accepted_indexes(statuses: Sequence[int]) -> list[int]:
    assert statuses and all(status in (200, 409) for status in statuses), "unexpected status"
    indexes = [index for index, status in enumerate(statuses) if status == 200]
    assert indexes, "no successful contender"
    return indexes
