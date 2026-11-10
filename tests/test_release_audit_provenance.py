from __future__ import annotations

import base64
from dataclasses import replace
import json

import pytest

from tests.test_release_audit import NOW, ROOT, facts, snapshot
from tools.release_audit_collect import collect_file
from tools.release_audit_evaluate import evaluate
from tools.release_audit_http import Response
from tools.release_audit_model import JSON, AuditError, Registry
from tools.release_audit_registry import load_registry


class ProvenanceFetcher:
    def __init__(self, value: JSON) -> None:
        self.value = value

    def get(self, host: str, path: str, accept: str = "application/vnd.github+json") -> Response:
        return Response(200, json.dumps({"attestation_bundles": [self.value]}))


def der(tag: int, value: bytes) -> bytes:
    size = len(value)
    length = bytes([size]) if size < 128 else bytes([0x82]) + size.to_bytes(2, "big")
    return bytes([tag]) + length + value


def bundle(
    filename: str = "package.whl", version: str = "0.12.2", digest: str = "a" * 64
) -> dict[str, JSON]:
    claims = {
        12: "https://github.com/yeongseon/azure-functions-langgraph-python",
        13: "a" * 40,
        14: "refs/tags/v" + version,
        21: "https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/123/attempts/1",
    }
    extensions = b"".join(
        der(
            0x30,
            der(0x06, bytes.fromhex("2b0601040183bf3001") + bytes([oid]))
            + der(0x04, der(0x0C, value.encode())),
        )
        for oid, value in claims.items()
    )
    certificate = der(0x30, der(0x30, der(0xA3, der(0x30, extensions))))
    statement: JSON = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://docs.pypi.org/attestations/publish/v1",
        "subject": [{"name": filename, "digest": {"sha256": digest}}],
    }
    return {
        "publisher": {
            "kind": "GitHub",
            "repository": "yeongseon/azure-functions-langgraph-python",
            "workflow": "publish-pypi.yml",
            "environment": "pypi",
        },
        "attestations": [
            {
                "envelope": {
                    "statement": base64.b64encode(json.dumps(statement).encode()).decode()
                },
                "verification_material": {"certificate": base64.b64encode(certificate).decode()},
            }
        ],
    }


def collected(value: JSON) -> tuple[JSON, ProvenanceFetcher]:
    file: JSON = {
        "filename": "package.whl",
        "packagetype": "bdist_wheel",
        "digests": {"sha256": "a" * 64},
        "upload_time_iso_8601": "2026-10-16T00:00:00Z",
        "yanked": False,
    }
    return file, ProvenanceFetcher(value)


def test_publisher_only_bundle_is_not_publication_proof() -> None:
    # Given: a bundle claiming a publisher but containing no attestations.
    value = bundle()
    value["attestations"] = []
    raw, fetcher = collected(value)
    file = collect_file(fetcher, ("azure-functions-langgraph", "0.12.2"), raw)
    original = facts()
    # When: evaluating the file with all other release evidence intact.
    result = evaluate(
        snapshot(replace(original, files=(file, original.files[1]))), replace_registry(), NOW
    )[0]
    # Then: publisher metadata cannot stand in for an attestation.
    assert "provenance-missing" in {f.code for f in result.findings}


def replace_registry() -> Registry:
    return replace(load_registry(ROOT), exceptions=())


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("subject_name", "other.whl", "provenance-subject-mismatch"),
        ("subject_sha256", "b" * 64, "provenance-subject-mismatch"),
        ("source_repository", "https://github.com/other/repo", "provenance-source-mismatch"),
        ("source_digest", "b" * 40, "provenance-source-mismatch"),
        ("source_ref", "refs/heads/main", "provenance-source-mismatch"),
        ("run_id", 999, "provenance-source-mismatch"),
    ],
)
def test_every_attestation_is_bound_to_file_source_and_run(
    field: str, value: str | int, code: str
) -> None:
    # Given: a second attestation contradicts one claim of valid publication proof.
    candidate = facts()
    original = candidate.files[0]
    attestation = original.attestations[0]
    altered = replace(
        attestation,
        subject_name=str(value) if field == "subject_name" else attestation.subject_name,
        subject_sha256=str(value) if field == "subject_sha256" else attestation.subject_sha256,
        source_repository=str(value)
        if field == "source_repository"
        else attestation.source_repository,
        source_digest=str(value) if field == "source_digest" else attestation.source_digest,
        source_ref=str(value) if field == "source_ref" else attestation.source_ref,
        run_id=int(value) if field == "run_id" else attestation.run_id,
    )
    candidate = replace(
        candidate,
        files=(
            replace(original, attestations=(*original.attestations, altered)),
            candidate.files[1],
        ),
    )
    # When: evaluating all attestations, not just the first one.
    result = evaluate(snapshot(candidate), replace_registry(), NOW)[0]
    # Then: every binding is mandatory for every attestation.
    assert result.verdict == "DEFECT"
    assert code in {f.code for f in result.findings}


@pytest.mark.parametrize(
    "damage",
    [
        "statement-base64",
        "statement-json",
        "type",
        "predicate",
        "subjects",
        "digest",
        "certificate",
        "missing-oid",
    ],
)
def test_malformed_attestation_is_audit_error(damage: str) -> None:
    # Given: malformed attestation material behind a legitimate publisher.
    from tools.release_audit_parse import mapping, sequence

    value = bundle()
    attestation = mapping(sequence(value["attestations"])[0])
    envelope = mapping(attestation["envelope"])
    material = mapping(attestation["verification_material"])
    if damage.startswith("statement"):
        envelope["statement"] = (
            "!" if damage == "statement-base64" else base64.b64encode(b"not json").decode()
        )
    elif damage in {"certificate", "missing-oid"}:
        material["certificate"] = base64.b64encode(
            b"bad" if damage == "certificate" else der(0x30, der(0x30, der(0xA3, der(0x30, b""))))
        ).decode()
    else:
        statement: JSON = json.loads(base64.b64decode(str(envelope["statement"])))
        parsed = mapping(statement)
        if damage == "type":
            parsed["_type"] = "wrong"
        if damage == "predicate":
            parsed["predicateType"] = "wrong"
        if damage == "subjects":
            parsed["subject"] = []
        if damage == "digest":
            mapping(sequence(parsed["subject"])[0])["digest"] = {"sha256": "garbage"}
        envelope["statement"] = base64.b64encode(json.dumps(statement).encode()).decode()
    raw, fetcher = collected(value)
    # When / Then: collection fails rather than silently discarding bad claims.
    with pytest.raises(AuditError):
        collect_file(fetcher, ("azure-functions-langgraph", "0.12.2"), raw)
