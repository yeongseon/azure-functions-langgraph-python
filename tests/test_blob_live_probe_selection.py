"""The opt-in fault lane must skip before resolving any network fixture."""

from __future__ import annotations

import inspect
from unittest.mock import Mock

import pytest

from tests.integration import test_blob_live_faults as live


def test_unset_live_selector_skips_before_requesting_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given no explicit live endpoint and a network fixture that must never be resolved.
    monkeypatch.delenv("BLOB_TEST_CONNECTION_STRING_ENV", raising=False)
    request = Mock()
    request.getfixturevalue.side_effect = AssertionError("must not contact storage")
    # When entering the fixture, then skip rather than fail or use an emulator.
    with pytest.raises(pytest.skip.Exception):
        next(inspect.unwrap(live.live_container)(request))
    request.getfixturevalue.assert_not_called()
