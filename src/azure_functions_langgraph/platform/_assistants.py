from __future__ import annotations

import json
from typing import Any

import azure.functions as func
from pydantic import ValidationError

from azure_functions_langgraph._validation import sanitized_validation_errors, validate_body_size
from azure_functions_langgraph.platform._common import (
    PlatformRouteDeps,
    _check_unknown_platform_fields,
    _platform_error,
    _registration_to_assistant,
    _result_limit_error,
)
from azure_functions_langgraph.platform.contracts import (
    Assistant,
    AssistantCount,
    AssistantSearch,
)


def _assistant_matches(reg_name: str, filters: AssistantSearch | AssistantCount) -> bool:
    """Match registered assistants, whose metadata is always ``None``.

    Absent, null and empty metadata impose no constraints. A non-empty
    exact key/value filter cannot match an assistant without metadata.
    """
    return (
        not filters.metadata
        and (filters.graph_id is None or reg_name == filters.graph_id)
        and (filters.name is None or filters.name.casefold() in reg_name.casefold())
    )


def register_assistant_routes(
    app: func.FunctionApp,
    deps: PlatformRouteDeps,
) -> None:
    auth = deps.auth_level

    @app.function_name(name="aflg_platform_assistants_search")
    @app.route(route="assistants/search", methods=["POST"], auth_level=auth)
    def assistants_search(req: func.HttpRequest) -> func.HttpResponse:
        raw = req.get_body()
        size_err = validate_body_size(raw, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(413, size_err)
        if raw and raw.strip() != b"":
            try:
                body: dict[str, Any] = req.get_json()
            except ValueError:
                return _platform_error(400, "Invalid JSON body")
        else:
            body = {}

        unknown_err = _check_unknown_platform_fields(AssistantSearch, body)
        if unknown_err is not None:
            return unknown_err
        try:
            search = AssistantSearch.model_validate(body)
        except ValidationError as exc:
            return _platform_error(422, sanitized_validation_errors(exc))
        limit_error = _result_limit_error(search.limit, deps.max_platform_result_limit)
        if limit_error is not None:
            return limit_error

        results: list[Assistant] = []
        for reg_name, reg in deps.registrations.items():
            if not _assistant_matches(reg_name, search):
                continue
            results.append(_registration_to_assistant(reg_name, reg))

        page = results[search.offset : search.offset + search.limit]
        return func.HttpResponse(
            body=json.dumps([a.model_dump(mode="json") for a in page], default=str),
            mimetype="application/json",
            status_code=200,
        )

    @app.function_name(name="aflg_platform_assistants_count")
    @app.route(route="assistants/count", methods=["POST"], auth_level=auth)
    def assistants_count(req: func.HttpRequest) -> func.HttpResponse:
        raw = req.get_body()
        size_err = validate_body_size(raw, deps.max_request_body_bytes)
        if size_err:
            return _platform_error(413, size_err)
        if raw and raw.strip() != b"":
            try:
                body: dict[str, Any] = req.get_json()
            except ValueError:
                return _platform_error(400, "Invalid JSON body")
        else:
            body = {}

        unknown_err = _check_unknown_platform_fields(AssistantCount, body)
        if unknown_err is not None:
            return unknown_err
        try:
            count_req = AssistantCount.model_validate(body)
        except ValidationError as exc:
            return _platform_error(422, sanitized_validation_errors(exc))

        total = sum(_assistant_matches(reg_name, count_req) for reg_name in deps.registrations)

        return func.HttpResponse(
            body=json.dumps(total),
            mimetype="application/json",
            status_code=200,
        )

    @app.function_name(name="aflg_platform_assistants_get")
    @app.route(route="assistants/{assistant_id}", methods=["GET"], auth_level=auth)
    def assistants_get(req: func.HttpRequest) -> func.HttpResponse:
        assistant_id = req.route_params.get("assistant_id", "")
        reg = deps.registrations.get(assistant_id)
        if reg is None:
            return _platform_error(404, f"Assistant {assistant_id!r} not found")
        assistant = _registration_to_assistant(assistant_id, reg)
        return func.HttpResponse(
            body=json.dumps(assistant.model_dump(mode="json"), default=str),
            mimetype="application/json",
            status_code=200,
        )
