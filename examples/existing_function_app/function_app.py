import azure.functions as func
from graph import graph

from azure_functions_langgraph import LangGraphApp

app = func.FunctionApp()


@app.function_name(name="existing_http")
@app.route(route="existing", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def existing_http(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("existing HTTP still works")


@app.function_name(name="existing_timer")
@app.timer_trigger(
    schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True
)
def existing_timer(timer: func.TimerRequest) -> None:
    pass


langgraph_app = LangGraphApp()
langgraph_app.register(graph=graph, name="official_basic")
app.register_functions(langgraph_app.to_blueprint())
