from datetime import date
from pathlib import Path

import pytest

from scripts.check_audit_exceptions import AuditExceptionError, parse_exceptions


def test_parse_exceptions_returns_active_ids(tmp_path: Path) -> None:
    # Given: a documented exception with a future expiry
    exceptions = tmp_path / "exceptions.txt"
    exceptions.write_text("GHSA-abcd-1234-efgh  # upstream fix pending  # expires 2999-01-01\n")

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
