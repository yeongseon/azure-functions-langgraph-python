"""Deterministic substitute for the guide's paid Anthropic model, not upstream code."""

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage


class StateAwareFakeModel:
    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        human_turns = sum(message.type == "human" for message in messages)
        return AIMessage(content=f"observed {human_turns} human turn(s)")


model = StateAwareFakeModel()
