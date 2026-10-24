"""Contract for the emulator Compose files.

``make test-table`` and ``make test-cosmos`` are the only users of these Compose files and no CI
job starts them, so this check is what keeps an emulator published on the loopback interface only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ["docker-compose.azurite.yml", "docker-compose.cosmos.yml"]


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_emulator_ports_are_published_on_loopback_only(compose_file: str) -> None:
    compose = cast(dict[str, Any], yaml.safe_load((ROOT / compose_file).read_text()))
    ports = [port for service in compose["services"].values() for port in service.get("ports", [])]

    assert ports, f"{compose_file} publishes no ports"
    assert [port for port in ports if not str(port).startswith("127.0.0.1:")] == []
