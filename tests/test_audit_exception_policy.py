from datetime import date
from pathlib import Path

import pytest

from scripts.check_audit_exceptions import AuditExceptionError, parse_exceptions


@pytest.mark.parametrize(
    "entry",
    [
        "GHSA-abcd-1234-efgh # reason # expires 2026-10-22",
        "GHSA-abcd-1234-efgh # reason # owner maintainer # expires 2026-10-23",
        "GHSA-abcd-1234-efgh # reason # owner maintainer # expires 2027-10-23",
        "GHSA-abcd-1234-efgh # reason # expires 2026-11-23",
        "--disable-pip # reason # owner maintainer # expires 2026-11-23",
        "GHSA-abcd-1234-efgh # reason # owner maintainer # expires 2026-99-23",
    ],
)
def test_invalid_exception_cannot_bypass_release(tmp_path: Path, entry: str) -> None:
    # Given: expired, unowned, overlong, malformed or command-like exceptions.
    path = tmp_path / "exceptions.txt"
    path.write_text(entry)
    # When / Then: the existing parser remains the mandatory fail-closed gate.
    with pytest.raises(AuditExceptionError):
        parse_exceptions(path, today=date(2026, 10, 23))
