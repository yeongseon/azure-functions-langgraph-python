"""Contract for the emulator Compose files and the Makefile targets that start them.

``make test-table`` and ``make test-cosmos`` are the only users of these Compose files and no CI
job starts them, so these checks are what keep the two promises the targets make: an emulator is
published on the loopback interface only, and a run only ever tears down the Compose project it
started itself.
"""

from __future__ import annotations

from pathlib import Path
import re
import shlex
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


def test_makefile_only_touches_the_compose_project_the_run_started() -> None:
    makefile = (ROOT / "Makefile").read_text().replace("\\\n", " ")
    calls = [shlex.split(call) for call in re.findall(r"docker compose ([^;'\n]+)", makefile)]
    names = re.findall(r'\bp="([^"]*)"', makefile)

    assert any("down" in call for call in calls), "the targets no longer clean up after a run"
    assert [call for call in calls if call[:2] != ["-p", "$$p"]] == []
    assert names and all(name.endswith("-$$$$") for name in names), "project names must be per run"
