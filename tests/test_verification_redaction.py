from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from tools.verification_evidence import Summary, redact, save


@pytest.mark.parametrize(
    "text",
    [
        '{"Authorization": "Bearer CANARY"}',
        "{'Authorization': 'Bearer CANARY'}",
        "Proxy-Authorization: Basic CANARY==",
        '{"password": "alpha CANARY beta"}',
        '{"token": "with \\"escaped\\" CANARY"}',
        "connection_string=Endpoint=sb://ns/;SharedAccessKeyName=Root;SharedAccessKey=CANARY",
        "InstrumentationKey=CANARY;IngestionEndpoint=https://x/",
        '<failure message="Authorization: Bearer CANARY">x</failure>',
        "https://user:CANARY@example.test/",
        "DefaultEndpointsProtocol=https;AccountKey=CANARY;EndpointSuffix=x",
        "GET /api?code=CANARY&x=1",
        "x-functions-key: CANARY",
    ],
)
def test_quoted_credentials_are_consumed_whole(text: str) -> None:
    # Given credential-shaped text, when redacting, then the whole value is hidden.
    assert "CANARY" not in redact(text)


def test_known_secrets_precede_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given overlapping and JSON-escaped secrets, when redacting, then neither leaks.
    monkeypatch.setenv("SHORT_TOKEN", "prefix")
    monkeypatch.setenv("LONG_TOKEN", 'prefix;value&CANARY "quoted"')
    assert "CANARY" not in redact(json.dumps({"x": 'prefix;value&CANARY "quoted"'}))


def test_save_keeps_nested_json_valid(tmp_path: Path) -> None:
    # Given nested evidence and tuple command fields, when saving, then JSON remains parseable.
    (tmp_path / "extra.json").write_text(
        '{"password": 123, "nested": ["Authorization: Bearer CANARY"]}'
    )
    save(tmp_path, Summary(1, "test", ('Authorization: "Bearer CANARY"',), 1, 1, 0, 0, "PASS", 0))
    for name in ("extra.json", "summary.json"):
        text = (tmp_path / name).read_text()
        assert "CANARY" not in text
        json.loads(text)


def test_save_preserves_binary_and_symlink_targets(tmp_path: Path) -> None:
    # Given non-evidence assets and external links, when saving, then they are untouched.
    png = b"\x89PNG\xff\xfe\x00"
    (tmp_path / "chart.png").write_bytes(png)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.log"
    outside.write_text("password=CANARY")
    (tmp_path / "link.log").symlink_to(outside)
    save(tmp_path, Summary(1, "test", (), 1, 1, 0, 0, "PASS", 0))
    assert (tmp_path / "chart.png").read_bytes() == png
    assert outside.read_text() == "password=CANARY"


def test_xml_redaction_preserves_attribute_quotes() -> None:
    # Given XML attributes, when redacting, then XML syntax survives.
    ET.fromstring(redact('<failure message="Authorization: Bearer CANARY">x</failure>'))


def test_redaction_preserves_json_and_ordinary_fields() -> None:
    # Given ordinary fields beside credentials, when redacting, then syntax and safe names survive.
    data = json.loads(redact('{"Authorization": "Bearer CANARY", "ok": true}'))
    assert data == {"Authorization": "[REDACTED]", "ok": True}
    text = "token_count=3 SharedAccessKeyName=Root author=ada"
    assert redact(text) == text
