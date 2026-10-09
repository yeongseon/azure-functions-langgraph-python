from __future__ import annotations

import os
import socket

import pytest


def fail_or_skip_service(message: str) -> None:
    if os.getenv("AZURITE_REQUIRED") == "true":
        pytest.fail(message)
    pytest.skip(message)


def require_tcp_service(host: str, port: int, service: str) -> None:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return
    except OSError as exc:
        fail_or_skip_service(f"{service} is unavailable at {host}:{port}: {exc}")
