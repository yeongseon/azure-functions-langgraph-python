"""Fail a ``-m e2e`` run that has no deployment to test, instead of letting it skip.

Every e2e module skips itself when its ``E2E_*`` input is unset, so a certification run that was
never pointed at a deployment ends with every test skipped and pytest exit status 0, which reads
as a pass (#686). Under the dedicated ``-m e2e`` selector a missing input is a configuration error.
Other runs are unaffected: collection only, the default ``-m 'not e2e'`` suite, and the
minimum-dependency lane (``-o addopts=''``), which collects these tests unselected and skips them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os

import pytest

# The inputs a module needs to do anything but skip. E2E_STREAM_CODE (the function key) is not
# listed: a local Functions host accepts an empty key, and a missing or wrong key against Azure
# fails the tests with a 401 instead of skipping them.
REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    "test_langgraph_e2e": ("E2E_BASE_URL",),
    "test_langgraph_lock_e2e": ("E2E_BASE_URL", "E2E_BASE_URL_B"),
    "test_true_streaming_e2e": ("E2E_STREAM_BASE_URL",),
}


def unmet_inputs(modules: Iterable[str], environ: Mapping[str, str]) -> list[str]:
    """One line per selected module that would skip, or that this guard does not know about."""
    lines: list[str] = []
    for module in sorted(set(modules)):
        required = REQUIRED_INPUTS.get(module)
        if required is None:
            lines.append(f"{module}: not listed in REQUIRED_INPUTS in tests/e2e/conftest.py")
            continue
        # The modules strip a trailing "/" before testing the value, so "/" alone is unset too.
        unset = [name for name in required if not environ.get(name, "").rstrip("/")]
        if unset:
            lines.append(f"{module}: {', '.join(unset)}")
    return lines


# trylast: run after the -m and -k deselection, so only the tests that will really run count.
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.option.collectonly or (config.option.markexpr or "").strip() != "e2e":
        return
    lines = unmet_inputs((item.path.stem for item in items), os.environ)
    if lines:
        details = "\n".join(f"  {line}" for line in lines)
        pytest.exit(
            "tests/e2e needs a deployment for every selected module; unset or empty inputs:\n"
            f"{details}\n"
            "A '-m e2e' run must not skip for want of them: skipped tests read as a pass (#686).",
            returncode=1,
        )
