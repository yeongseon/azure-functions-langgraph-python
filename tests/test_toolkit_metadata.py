"""Tests for the _azure_functions_toolkit_metadata convention on LangGraphApp handlers."""

from __future__ import annotations

from azure_functions_langgraph.app import LangGraphApp, get_langgraph_metadata
from tests.conftest import FakeCompiledGraph, FakeStatefulGraph


class TestHandlerToolkitMetadata:
    """Verify that handler functions created by LangGraphApp get toolkit metadata."""

    def test_invoke_handler_has_metadata(self) -> None:
        """Invoke handler gets langgraph metadata with graph_name and endpoint."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        func_app = app.function_app

        # Find the invoke handler function
        functions = _get_registered_functions(func_app)
        invoke_fn = functions.get("aflg_agent_invoke")
        assert invoke_fn is not None

        metadata = getattr(invoke_fn, "_azure_functions_metadata", None)
        assert metadata is not None
        assert "langgraph" in metadata
        meta = metadata["langgraph"]
        assert meta["version"] == 1
        assert meta["graph_name"] == "agent"
        assert meta["endpoint"] == "invoke"

    def test_stream_handler_has_metadata(self) -> None:
        """Stream handler gets langgraph metadata."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        func_app = app.function_app

        functions = _get_registered_functions(func_app)
        stream_fn = functions.get("aflg_agent_stream")
        assert stream_fn is not None

        metadata = getattr(stream_fn, "_azure_functions_metadata", None)
        assert metadata is not None
        meta = metadata["langgraph"]
        assert meta["version"] == 1
        assert meta["graph_name"] == "agent"
        assert meta["endpoint"] == "stream"

    def test_state_handler_has_metadata(self) -> None:
        """State handler on stateful graph gets langgraph metadata."""
        app = LangGraphApp()
        app.register(graph=FakeStatefulGraph(), name="stateful")
        func_app = app.function_app

        functions = _get_registered_functions(func_app)
        state_fn = functions.get("aflg_stateful_state")
        assert state_fn is not None

        metadata = getattr(state_fn, "_azure_functions_metadata", None)
        assert metadata is not None
        meta = metadata["langgraph"]
        assert meta["version"] == 1
        assert meta["graph_name"] == "stateful"
        assert meta["endpoint"] == "state"

    def test_multiple_graphs_each_get_metadata(self) -> None:
        """Each graph's handlers get independent metadata."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="alpha")
        app.register(graph=FakeCompiledGraph(), name="beta")
        func_app = app.function_app

        functions = _get_registered_functions(func_app)

        alpha_invoke = functions.get("aflg_alpha_invoke")
        beta_invoke = functions.get("aflg_beta_invoke")
        assert alpha_invoke is not None
        assert beta_invoke is not None

        alpha_meta = getattr(alpha_invoke, "_azure_functions_metadata")["langgraph"]
        beta_meta = getattr(beta_invoke, "_azure_functions_metadata")["langgraph"]

        assert alpha_meta["graph_name"] == "alpha"
        assert beta_meta["graph_name"] == "beta"


class TestGetLanggraphMetadata:
    """Verify the get_langgraph_metadata getter function."""

    def test_returns_metadata_for_handler(self) -> None:
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        func_app = app.function_app

        functions = _get_registered_functions(func_app)
        invoke_fn = functions.get("aflg_agent_invoke")

        result = get_langgraph_metadata(invoke_fn)
        assert result is not None
        assert result["version"] == 1
        assert result["graph_name"] == "agent"

    def test_returns_none_for_plain_function(self) -> None:
        def plain() -> None:
            pass

        result = get_langgraph_metadata(plain)
        assert result is None

    def test_returns_none_for_wrong_namespace(self) -> None:
        def handler() -> None:
            pass

        setattr(handler, "_azure_functions_metadata", {"db": {"version": 1}})
        result = get_langgraph_metadata(handler)
        assert result is None

    def test_returns_none_for_non_dict(self) -> None:
        def handler() -> None:
            pass

        setattr(handler, "_azure_functions_metadata", "not-a-dict")
        result = get_langgraph_metadata(handler)
        assert result is None


class TestMetadataPreservesNamespaces:
    """Verify that langgraph metadata preserves other toolkit namespaces."""

    def test_preserves_existing_namespaces(self) -> None:
        """Manually set metadata on a handler is preserved when langgraph adds its own."""
        app = LangGraphApp()
        app.register(graph=FakeCompiledGraph(), name="agent")
        func_app = app.function_app

        functions = _get_registered_functions(func_app)
        invoke_fn = functions.get("aflg_agent_invoke")
        assert invoke_fn is not None

        # Manually add another namespace before checking
        existing = getattr(invoke_fn, "_azure_functions_metadata", {})
        existing["db"] = {"version": 1, "bindings": []}
        setattr(invoke_fn, "_azure_functions_metadata", existing)

        metadata = getattr(invoke_fn, "_azure_functions_metadata")
        assert "db" in metadata
        assert "langgraph" in metadata


class TestLanggraphMetadataImport:
    """Verify get_langgraph_metadata is importable from the package."""

    def test_importable_from_package(self) -> None:
        import azure_functions_langgraph

        assert hasattr(azure_functions_langgraph, "get_langgraph_metadata")
        assert callable(azure_functions_langgraph.get_langgraph_metadata)

    def test_in_all(self) -> None:
        import azure_functions_langgraph

        assert "get_langgraph_metadata" in azure_functions_langgraph.__all__


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _get_registered_functions(func_app: object) -> dict[str, object]:
    """Extract registered function objects from a FunctionApp by name.

    The Azure Functions SDK stores function objects in an internal registry.
    We inspect the FunctionApp to find the user-defined handler functions.
    """
    # FunctionApp stores functions in _function_builders or similar.
    # We iterate over common internal structures.
    functions: dict[str, object] = {}

    # azure.functions.FunctionApp uses _function_builders list
    builders = getattr(func_app, "_function_builders", [])
    for builder in builders:
        # Each builder has a function with the decorated handler
        fn = getattr(builder, "_function", None)
        if fn is not None:
            fn_name = getattr(fn, "get_function_name", lambda: None)()
            user_fn = getattr(fn, "get_user_function", lambda: None)()
            if fn_name and user_fn:
                functions[fn_name] = user_fn

    return functions
