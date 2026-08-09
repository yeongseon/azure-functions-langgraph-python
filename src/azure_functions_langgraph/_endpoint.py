"""Builder for the cross-package ``endpoint`` metadata namespace.

Toolkit convention (shared across the Azure Functions Python DX Toolkit):
handlers carry an ``_azure_functions_metadata`` dict keyed by a package-owned
*namespace* string, so sibling packages (e.g. ``azure-functions-openapi``) can
discover metadata **without importing this package**.

The ``"endpoint"`` namespace is the shared, OpenAPI-ready contract. Unlike the
``"langgraph"`` namespace (which carries package-internal data), the
``endpoint`` payload is entirely *self-contained* JSON Schema: the consumer
needs no import of this package and no access to the user's model classes.

The payload shape and canonicalization rules follow a shared toolkit
convention (see the discussion linked below); no single package *owns* the
``endpoint`` namespace. This module is fully self-contained and independently
adopts the same ``by_alias``/``ref_template``/``mode`` canonicalization so its
emitted JSON Schema stays byte-consistent with sibling producers/consumers —
it is NOT shared via a runtime dependency. Keep the ``version`` field, that
canonicalization, and the merge-without-clobber semantics identical to the
sibling packages.

Ref: https://github.com/yeongseon/azure-functions-validation-python/issues/270
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict, TypeGuard

from pydantic import BaseModel

#: Convention attribute name shared across all toolkit packages.
METADATA_ATTR = "_azure_functions_metadata"

#: Namespace for the shared endpoint convention (no single owning package).
ENDPOINT_NAMESPACE = "endpoint"

#: Schema version for the ``endpoint`` namespace payload.
ENDPOINT_METADATA_VERSION = 1

#: Pydantic ref template chosen to match the shared convention so ``$defs`` stay
#: unresolved and the consumer (openapi) remains the sole ``$ref``-collision
#: authority.
_REF_TEMPLATE = "#/$defs/{model}"

#: Pydantic JSON-Schema generation modes (request vs response canonicalization).
_SchemaMode = Literal["validation", "serialization"]


class EndpointMetadata(TypedDict):
    """Shape of ``_azure_functions_metadata["endpoint"]`` (schema version 1).

    Total: ``build_endpoint_metadata`` always emits every key, and consumers
    index them directly, so all fields are required by the cross-package
    contract.
    """

    version: int
    request_body: dict[str, Any] | None
    request_body_required: bool
    parameters: list[dict[str, Any]]
    responses: dict[str, dict[str, Any]] | None


def _is_model_type(model: object) -> TypeGuard[type[BaseModel]]:
    """Return ``True`` if *model* is a Pydantic ``BaseModel`` subclass."""
    return isinstance(model, type) and issubclass(model, BaseModel)


def _model_schema(model: type[BaseModel], mode: _SchemaMode) -> dict[str, Any]:
    """Generate a model's JSON Schema using the shared canonicalization.

    LangGraph independently applies ``by_alias``/``ref_template``/``mode`` here
    to keep its emitted schema consistent with sibling producers/consumers,
    matching the toolkit convention rather than a package-owned SPEC mandate.
    """
    return model.model_json_schema(
        by_alias=True,
        ref_template=_REF_TEMPLATE,
        mode=mode,
    )


def build_endpoint_metadata(
    *,
    request_model: type[BaseModel] | None,
    response_model: type[BaseModel] | None,
    parameters: list[dict[str, Any]] | None = None,
    success_status_code: int = 200,
) -> EndpointMetadata:
    """Build the ``endpoint`` namespace payload for a single HTTP route.

    ``request_body`` is the request model's JSON Schema in ``"validation"`` mode;
    ``responses`` maps the success status code to the response model's JSON Schema
    in ``"serialization"`` mode. ``parameters`` is passed through verbatim (each
    entry is a self-contained OpenAPI parameter object).
    """
    if _is_model_type(request_model):
        request_body: dict[str, Any] | None = _model_schema(request_model, "validation")
        request_body_required = any(
            field.is_required() for field in request_model.model_fields.values()
        )
    else:
        request_body = None
        request_body_required = False

    if _is_model_type(response_model):
        status = str(success_status_code or 200)
        responses: dict[str, dict[str, Any]] | None = {
            status: {"schema": _model_schema(response_model, "serialization")}
        }
    else:
        responses = None

    payload: EndpointMetadata = {
        "version": ENDPOINT_METADATA_VERSION,
        "request_body": request_body,
        "request_body_required": request_body_required,
        "parameters": list(parameters) if parameters else [],
        "responses": responses,
    }
    return payload


def set_endpoint_metadata(handler: Any, payload: EndpointMetadata) -> None:
    """Merge the ``endpoint`` namespace onto *handler* without clobbering others.

    Reads any pre-existing convention attribute (e.g. the ``langgraph`` namespace
    already written by :func:`set_langgraph_metadata`), merges in *payload* under
    the ``endpoint`` namespace, and writes the result back onto *handler*.
    """
    existing = getattr(handler, METADATA_ATTR, None)
    base: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    base[ENDPOINT_NAMESPACE] = payload
    setattr(handler, METADATA_ATTR, base)
