from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import azure.functions as func
import pytest

pytest.importorskip("azure.storage.blob")
from azure.core.exceptions import ResourceNotFoundError

from azure_functions_langgraph.authorization import (
    AuthorizationAction,
    AuthorizationContext,
    UnauthenticatedError,
)
from tests.example_imports import load_example_file

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "existing_function_app"
TENANT = "11111111-1111-1111-1111-111111111111"
OWNER = "22222222-2222-2222-2222-222222222222"
OTHER = "33333333-3333-3333-3333-333333333333"


def request(principal: str) -> func.HttpRequest:
    claims = {
        "auth_typ": "aad",
        "claims": [
            {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": TENANT},
            {
                "typ": "http://schemas.microsoft.com/identity/claims/objectidentifier",
                "val": principal,
            },
            {"typ": "name", "val": "not-an-owner"},
        ],
    }
    header = base64.b64encode(json.dumps(claims).encode()).decode()
    return func.HttpRequest("GET", "/", headers={"x-ms-client-principal": header}, body=b"")


@pytest.mark.parametrize("action", [AuthorizationAction.RUN, AuthorizationAction.STATE_READ])
@pytest.mark.parametrize("principal,allowed", [(OWNER, True), (OTHER, False)])
def test_server_owner_controls_admission(
    action: AuthorizationAction, principal: str, allowed: bool
) -> None:
    # Given a stored owner and the same thread addressed by distinct principals.
    module = load_example_file(EXAMPLE / "reference_policy.py", "_reference_policy")
    container = Mock()
    container.get_blob_client.return_value.download_blob.return_value.readall.return_value = (
        json.dumps({"tenant": TENANT, "principal": OWNER}).encode()
    )
    policy = module.BlobOwnerPolicy(container, UUID(TENANT))
    context = AuthorizationContext(action, "official_basic", "test-thread", request(principal))
    # When admitting, then both RUN and STATE_READ enforce the stored owner.
    assert policy(context) is allowed
    name = hashlib.sha256(b"official_basic:test-thread").hexdigest()
    container.get_blob_client.assert_called_once_with(f"owners/{name}.json")
    container.upload_blob.assert_not_called()


@pytest.mark.parametrize("header", ["", "not-base64", base64.b64encode(b"{}").decode()])
def test_malformed_ingress_is_unauthenticated(header: str) -> None:
    module = load_example_file(EXAMPLE / "reference_policy.py", "_reference_policy_bad")
    req = func.HttpRequest("GET", "/", headers={"x-ms-client-principal": header}, body=b"")
    with pytest.raises(UnauthenticatedError):
        module.verified_owner(req)


def test_missing_owner_record_denies_without_creating_it() -> None:
    module = load_example_file(EXAMPLE / "reference_policy.py", "_reference_policy_missing")
    container = Mock()
    container.get_blob_client.return_value.download_blob.side_effect = ResourceNotFoundError()
    policy = module.BlobOwnerPolicy(container, UUID(TENANT))
    assert (
        policy(
            AuthorizationContext(
                AuthorizationAction.RUN, "official_basic", "missing", request(OWNER)
            )
        )
        is False
    )
    container.upload_blob.assert_not_called()


@pytest.mark.parametrize("graph,thread", [("different", "test-thread"), ("official_basic", None)])
def test_graph_and_threadless_runs_fail_closed(graph: str, thread: str | None) -> None:
    module = load_example_file(EXAMPLE / "reference_policy.py", "_reference_policy_graph")
    container = Mock()
    policy = module.BlobOwnerPolicy(container, UUID(TENANT))
    assert (
        policy(AuthorizationContext(AuthorizationAction.RUN, graph, thread, request(OWNER)))
        is False
    )
    container.get_blob_client.assert_not_called()
