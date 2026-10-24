from datetime import date
import importlib.util
from pathlib import Path
import sys

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_audit_exceptions.py"
_spec = importlib.util.spec_from_file_location("check_audit_exceptions", _SCRIPT_PATH)
assert _spec and _spec.loader
check_audit_exceptions = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check_audit_exceptions
_spec.loader.exec_module(check_audit_exceptions)

AuditExceptionError = check_audit_exceptions.AuditExceptionError
parse_exceptions = check_audit_exceptions.parse_exceptions


def test_parse_exceptions_returns_active_ids(tmp_path: Path) -> None:
    # Given: a documented exception with a future expiry
    exceptions = tmp_path / "exceptions.txt"
    exceptions.write_text(
        "GHSA-abcd-1234-efgh # upstream fix pending # owner maintainer # expires 2026-11-01\n"
    )

    # When: the exception file is parsed
    result = parse_exceptions(exceptions, today=date(2026, 10, 11))

    # Then: the scanner-ready vulnerability ID is returned
    assert result == ("GHSA-abcd-1234-efgh",)


@pytest.mark.parametrize(
    "entry",
    [
        "GHSA-abcd-1234-efgh  # expires 2999-01-01",
        "GHSA-abcd-1234-efgh  # temporary  # expires someday",
        "GHSA-abcd-1234-efgh  # temporary  # expires 2026-10-11",
    ],
)
def test_parse_exceptions_rejects_undocumented_invalid_or_expired_entries(
    tmp_path: Path, entry: str
) -> None:
    # Given: an exception that cannot justify an active release bypass
    exceptions = tmp_path / "exceptions.txt"
    exceptions.write_text(f"{entry}\n")

    # When / Then: validation rejects the entry
    with pytest.raises(AuditExceptionError):
        parse_exceptions(exceptions, today=date(2026, 10, 11))
