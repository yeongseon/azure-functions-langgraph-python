from __future__ import annotations

from pathlib import Path
import shutil
import socket
import subprocess

import pytest

from tools import verification_services
from tools.verification_services import MissingPrerequisite, compose, require_free

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("backend", ["cosmos", "azurite"])
def test_compose_publications_are_loopback_bound(backend: str) -> None:
    # Given either Compose definition, when inspecting publications, then all bind loopback.
    lines = (ROOT / f"docker-compose.{backend}.yml").read_text().split("    ports:\n")[1]
    publications = lines.split("    environment:")[0].split("    healthcheck:")[0].splitlines()
    assert publications
    assert all('"127.0.0.1:${' in line and ":-" in line for line in publications)


def test_cleanup_is_scoped_to_owned_project_even_after_failed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a unique unused project and free ports, but failed emulator startup.
    calls: list[list[str]] = []

    def fake_run(command: list[str], log: Path, timeout: float = 600) -> int:
        calls.append(command)
        return int("up" in command)

    monkeypatch.setattr(verification_services, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(verification_services, "require_free", lambda _: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )
    # When startup fails, then cleanup uses exactly that project's identity, never a shared one.
    with pytest.raises(MissingPrerequisite, match="cannot start"):
        with compose("azurite", tmp_path):
            pytest.fail("unready service must not yield")
    project = calls[0][3]
    assert project.startswith("lg-verify-")
    assert all(command[3] == project for command in calls)
    assert calls[-1][6:] == ["down", "--volumes", "--timeout", "10"]


def test_occupied_port_is_refused_without_modifying_service() -> None:
    # Given an existing listener, when preflighting, then refuse it without touching it.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        number = listener.getsockname()[1]
        with pytest.raises(MissingPrerequisite, match="occupied"):
            require_free((number,))
        assert listener.fileno() >= 0


def test_preexisting_project_is_never_adopted_or_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "shared-container", "")
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(verification_services, "run", lambda command, *a: calls.append(command))
    with pytest.raises(MissingPrerequisite, match="refusing adoption"):
        with compose("azurite", tmp_path):
            pytest.fail("must not adopt preexisting resources")
    assert calls == []
