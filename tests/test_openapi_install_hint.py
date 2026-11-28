import sys

import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.openapi import register_with_openapi


def test_install_hint_when_companion_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the companion is unavailable, even if installed in this environment.
    monkeypatch.setitem(sys.modules, "azure_functions_openapi", None)
    app = LangGraphApp()

    # When: a caller requests optional OpenAPI registration.
    with pytest.raises(ImportError) as error:
        register_with_openapi(app)

    # Then: the error recommends the adapter extra and retains the import failure.
    assert 'pip install "azure-functions-langgraph[openapi]"' in str(error.value)
    assert isinstance(error.value.__cause__, ModuleNotFoundError)
    assert error.value.__cause__.name == "azure_functions_openapi"
