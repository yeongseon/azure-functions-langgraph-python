"""Tests for contracts (request/response models)."""

from __future__ import annotations

from azure_functions_langgraph.contracts import (
    ErrorResponse,
    GraphInfo,
    HealthResponse,
    InvokeRequest,
    InvokeResponse,
    StreamRequest,
)


class TestInvokeRequest:
    def test_minimal(self) -> None:
        req = InvokeRequest(input={"messages": [{"role": "human", "content": "hi"}]})
        assert req.input["messages"][0]["content"] == "hi"
        assert req.config is None

    def test_with_config(self) -> None:
        req = InvokeRequest(
            input={"messages": []},
            config={"configurable": {"thread_id": "t1"}},
        )
        assert req.config is not None
        assert req.config["configurable"]["thread_id"] == "t1"


class TestStreamRequest:
    def test_defaults(self) -> None:
        req = StreamRequest(input={"messages": []})
        assert req.stream_mode == "values"
        assert req.config is None

    def test_custom_stream_mode(self) -> None:
        req = StreamRequest(input={"messages": []}, stream_mode="updates")
        assert req.stream_mode == "updates"


class TestInvokeResponse:
    def test_output(self) -> None:
        resp = InvokeResponse(output={"result": "done"})
        assert resp.output["result"] == "done"

    def test_json_serializable(self) -> None:
        resp = InvokeResponse(output={"k": "v"})
        data = resp.model_dump_json()
        assert '"k"' in data


class TestGraphInfo:
    def test_defaults(self) -> None:
        info = GraphInfo(name="agent")
        assert info.name == "agent"
        assert info.description is None
        assert info.has_checkpointer is False

    def test_with_checkpointer(self) -> None:
        info = GraphInfo(name="agent", has_checkpointer=True)
        assert info.has_checkpointer is True


class TestHealthResponse:
    def test_defaults(self) -> None:
        resp = HealthResponse()
        assert resp.status == "ok"
        assert resp.graphs == []

    def test_with_graphs(self) -> None:
        resp = HealthResponse(graphs=[GraphInfo(name="a"), GraphInfo(name="b")])
        assert len(resp.graphs) == 2


class TestErrorResponse:
    def test_minimal(self) -> None:
        resp = ErrorResponse(error="bad")
        assert resp.error == "bad"
        assert resp.detail is None

    def test_with_detail(self) -> None:
        resp = ErrorResponse(error="bad", detail="more info")
        assert resp.detail == "more info"
