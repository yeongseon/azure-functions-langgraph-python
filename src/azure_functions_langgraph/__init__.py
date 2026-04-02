"""Azure Functions LangGraph — Deploy LangGraph agents as Azure Functions."""

__version__ = "0.1.0a0"


def __getattr__(name: str) -> object:
    if name == "LangGraphApp":
        try:
            from azure_functions_langgraph.app import LangGraphApp

            return LangGraphApp
        except ImportError as exc:
            raise ImportError(
                "LangGraphApp requires 'azure-functions' and 'langgraph'. "
                "Install them with: pip install azure-functions-langgraph"
            ) from exc
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["LangGraphApp", "__version__"]
