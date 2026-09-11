"""True-streaming agent — Azure Functions entry point.

Serves a LangGraph graph with **true** incremental HTTP streaming using
``StreamingLangGraphApp``. Each event the graph emits is flushed to the client
as it is produced (``event: data`` frames), followed by a terminal ``event: end``.

> **App-wide ASGI mode.** Enabling true streaming switches the **entire**
> function app to the FastAPI/ASGI streaming model
> (``azurefunctions-extensions-http-fastapi``). It cannot be mixed with the
> classic ``HttpRequest``/``HttpResponse`` routes used by ``LangGraphApp`` — a
> single app is either classic *or* streaming, not both. If you need the classic
> buffered surface too, deploy it as a separate function app.

Prerequisites:
    - Azure Functions runtime **4.34.1+**
    - The ``streaming`` extra: ``pip install "azure-functions-langgraph[streaming]"``
    - App setting ``PYTHON_ENABLE_INIT_INDEXING=1``

Run from this directory:
    func start
"""

from __future__ import annotations

import azure.functions as func
from graph import compiled_graph

from azure_functions_langgraph.streaming import StreamingLangGraphApp

langgraph_app = StreamingLangGraphApp(auth_level=func.AuthLevel.FUNCTION)

langgraph_app.register(
    graph=compiled_graph,
    name="true_streaming_agent",
)


app = langgraph_app.function_app
