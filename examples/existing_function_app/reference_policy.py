"""Easy Auth admission policy; decoding is safe only behind mandatory trusted ingress."""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Literal
from uuid import UUID

from azure.core.exceptions import ResourceNotFoundError
import azure.functions as func
from azure.storage.blob import ContainerClient
from pydantic import BaseModel, ConfigDict, ValidationError

from azure_functions_langgraph.authorization import AuthorizationContext, UnauthenticatedError


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)
    typ: str
    val: str


class IngressPrincipal(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)
    auth_typ: Literal["aad"]
    claims: tuple[Claim, ...]


class OwnerRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    tenant: UUID
    principal: UUID


def verified_owner(request: func.HttpRequest) -> OwnerRecord:
    """Parse platform-injected identity, never caller-selected names or body fields."""
    try:
        encoded = request.headers.get("x-ms-client-principal", "")
        principal = IngressPrincipal.model_validate_json(base64.b64decode(encoded, validate=True))
        tenants = [
            c.val
            for c in principal.claims
            if c.typ in {"tid", "http://schemas.microsoft.com/identity/claims/tenantid"}
        ]
        objects = [
            c.val
            for c in principal.claims
            if c.typ in {"oid", "http://schemas.microsoft.com/identity/claims/objectidentifier"}
        ]
        if len(tenants) != 1 or len(objects) != 1:
            raise UnauthenticatedError
        return OwnerRecord(tenant=UUID(tenants[0]), principal=UUID(objects[0]))
    except (binascii.Error, ValueError, ValidationError):
        raise UnauthenticatedError from None


class BlobOwnerPolicy:
    """Read provisioned owner records per admission; requests cannot create or transfer owners."""

    def __init__(self, container: ContainerClient, tenant: UUID) -> None:
        self.container = container
        self.tenant = tenant

    def __call__(self, context: AuthorizationContext) -> bool:
        owner = verified_owner(context.request)
        if (
            owner.tenant != self.tenant
            or context.graph_name != "official_basic"
            or not context.thread_id
        ):
            return False
        name = hashlib.sha256(f"{context.graph_name}:{context.thread_id}".encode()).hexdigest()
        try:
            stored = self.container.get_blob_client(f"owners/{name}.json").download_blob().readall()
        except ResourceNotFoundError:
            return False
        return OwnerRecord.model_validate_json(stored) == owner
