from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from typing import assert_never

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ModelMode(StrEnum):
    FAKE = "fake"
    AZURE_OPENAI = "azure_openai"


@dataclass(frozen=True, slots=True)
class AzureOpenAIConfig:
    endpoint: str
    deployment: str
    api_version: str
    api_key: str | None
    use_entra_id: bool


def load_model_mode() -> ModelMode:
    raw_mode = os.environ.get("AGENT_MODEL_MODE", "")
    try:
        return ModelMode(raw_mode)
    except ValueError as exc:
        message = "AGENT_MODEL_MODE must be set to fake or azure_openai"
        raise ValueError(message) from exc


def load_azure_openai_config(mode: ModelMode) -> AzureOpenAIConfig | None:
    match mode:
        case ModelMode.FAKE:
            return None
        case ModelMode.AZURE_OPENAI:
            endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
            deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
            api_key = os.environ.get("AZURE_OPENAI_API_KEY") or None
            use_entra_id = (
                os.environ.get("AZURE_OPENAI_USE_ENTRA_ID", "").strip().lower() in _TRUTHY
            )
            missing = [
                name
                for name, present in (
                    ("AZURE_OPENAI_ENDPOINT", bool(endpoint)),
                    ("AZURE_OPENAI_DEPLOYMENT", bool(deployment)),
                )
                if not present
            ]
            if not api_key and not use_entra_id:
                missing.extend(("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_USE_ENTRA_ID"))
            if missing:
                message = f"Missing Azure OpenAI settings: {', '.join(missing)}"
                raise RuntimeError(message)
            return AzureOpenAIConfig(
                endpoint=endpoint,
                deployment=deployment,
                api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
                api_key=api_key,
                use_entra_id=use_entra_id,
            )
        case unreachable:
            assert_never(unreachable)
