"""Read PyPI-served claims, not Sigstore signature or transparency verification."""

from __future__ import annotations

import base64
import binascii
import json
import re

from tools.release_audit_model import JSON, Attestation, AuditError, Publisher
from tools.release_audit_parse import load, mapping, positive_id, sequence, sha256, text


def elements(data: bytes) -> tuple[tuple[int, bytes], ...]:
    result: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise AuditError("truncated DER header")
        tag, length = data[offset : offset + 2]
        offset += 2
        if length & 0x80:
            count = length & 0x7F
            if count == 0 or count > 4 or offset + count > len(data):
                raise AuditError("invalid DER length")
            length = int.from_bytes(data[offset : offset + count], "big")
            offset += count
        if offset + length > len(data):
            raise AuditError("truncated DER value")
        result.append((tag, data[offset : offset + length]))
        offset += length
    return tuple(result)


def single(data: bytes, tag: int) -> bytes:
    values = elements(data)
    if len(values) != 1 or values[0][0] != tag:
        raise AuditError("unexpected DER structure")
    return values[0][1]


def certificate_claims(certificate: bytes) -> dict[int, str]:
    outer = elements(single(certificate, 0x30))
    if not outer or outer[0][0] != 0x30:
        raise AuditError("missing tbsCertificate")
    extensions = [value for tag, value in elements(outer[0][1]) if tag == 0xA3]
    if len(extensions) != 1:
        raise AuditError("missing certificate extensions")
    claims: dict[int, str] = {}
    for tag, extension in elements(single(extensions[0], 0x30)):
        if tag != 0x30:
            raise AuditError("invalid certificate extension")
        fields = elements(extension)
        tags = tuple(t for t, _ in fields)
        if tags not in {(0x06, 0x04), (0x06, 0x01, 0x04)}:
            raise AuditError("invalid extension fields")
        oid = fields[0][1]
        prefix = bytes.fromhex("2b0601040183bf3001")
        if len(oid) == len(prefix) + 1 and oid[:-1] == prefix and oid[-1] in {12, 13, 14, 21}:
            if oid[-1] in claims:
                raise AuditError("duplicate Fulcio claim")
            claims[oid[-1]] = text(single(fields[-1][1], 0x0C).decode("utf-8"))
    if claims.keys() != {12, 13, 14, 21}:
        raise AuditError("missing Fulcio source/run claims")
    return claims


def commit_digest(value: JSON) -> str:
    digest = text(value)
    if not re.fullmatch(r"[a-fA-F0-9]{40}", digest):
        raise AuditError("source digest must have 40 hexadecimal digits")
    return digest.lower()


def parse_attestation(value: JSON, publisher: Publisher) -> Attestation:
    try:
        data = mapping(value)
        statement = mapping(
            load(
                base64.b64decode(
                    text(mapping(data["envelope"])["statement"]), validate=True
                ).decode("utf-8")
            )
        )
        if (
            statement["_type"] != "https://in-toto.io/Statement/v1"
            or statement["predicateType"] != "https://docs.pypi.org/attestations/publish/v1"
        ):
            raise AuditError("unexpected attestation statement type")
        subjects = sequence(statement["subject"])
        if len(subjects) != 1:
            raise AuditError("attestation must have exactly one subject")
        subject = mapping(subjects[0])
        certificate = base64.b64decode(
            text(mapping(data["verification_material"])["certificate"]), validate=True
        )
        claims = certificate_claims(certificate)
        invocation = re.fullmatch(
            r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/([1-9]\d*)/attempts/[1-9]\d*",
            claims[21],
        )
        if invocation is None:
            raise AuditError("invalid run invocation URI")
        return Attestation(
            text(subject["name"]),
            sha256(mapping(subject["digest"])["sha256"]),
            claims[12],
            commit_digest(claims[13]),
            claims[14],
            positive_id(int(invocation[1])),
            publisher,
        )
    except (KeyError, binascii.Error, UnicodeError, json.JSONDecodeError) as error:
        raise AuditError("malformed PyPI attestation") from error
