from __future__ import annotations

from collections.abc import Iterable, Iterator
import importlib
import os
from typing import Callable, Protocol, cast
import uuid

import pytest

EMULATOR_KEY = (
    "C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=="
)


@pytest.fixture
def cosmos_emulator_target() -> Iterator[tuple[str, str, str, str]]:
    endpoint = os.getenv("COSMOS_EMULATOR_ENDPOINT", "https://localhost:8081")
    _ = os.environ.setdefault("COSMOS_DISABLE_SSL", "true")

    class _Database(Protocol):
        def create_container_if_not_exists(self, *, id: str, partition_key: object) -> object: ...

    class _Client(Protocol):
        def list_databases(self) -> Iterable[object]: ...

        def create_database_if_not_exists(self, *, id: str) -> _Database: ...

        def delete_database(self, database: str) -> object: ...

    try:
        cosmos_module = importlib.import_module("azure.cosmos")
        exceptions_module = importlib.import_module("azure.cosmos.exceptions")
    except Exception as exc:
        pytest.skip(f"Cosmos dependencies unavailable: {exc}")

    CosmosClient = cast(Callable[..., object], getattr(cosmos_module, "CosmosClient"))
    PartitionKey = cast(Callable[..., object], getattr(cosmos_module, "PartitionKey"))
    CosmosHttpResponseError = cast(
        type[Exception], getattr(exceptions_module, "CosmosHttpResponseError")
    )

    client: _Client
    try:
        client = cast(
            _Client,
            CosmosClient(url=endpoint, credential=EMULATOR_KEY, connection_verify=False),
        )
        _ = list(client.list_databases())
    except Exception as exc:
        pytest.skip(f"Cosmos emulator not available: {exc}")

    database_name = f"aflg-int-{uuid.uuid4().hex[:12]}"
    container_name = f"checkpoints-{uuid.uuid4().hex[:8]}"

    try:
        database = client.create_database_if_not_exists(id=database_name)
        _ = database.create_container_if_not_exists(
            id=container_name,
            partition_key=PartitionKey(path="/partition_key"),
        )
    except CosmosHttpResponseError as exc:
        pytest.skip(f"Cosmos emulator not available: {exc}")

    try:
        yield endpoint, EMULATOR_KEY, database_name, container_name
    finally:
        try:
            _ = client.delete_database(database_name)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _patch_cosmos_client_for_emulator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch CosmosClient in the upstream package to disable SSL verification.

    The Cosmos emulator uses a self-signed certificate.  The upstream
    ``CosmosDBSaver.__init__`` does not pass ``connection_verify=False``
    to ``CosmosClient``, so we patch it at the module level.
    """
    try:
        import azure.cosmos
        import langgraph_checkpoint_cosmosdb.cosmosdbSaver as saver_mod
    except ImportError:
        return

    _OriginalClient = azure.cosmos.CosmosClient

    class _NoVerifyClient(_OriginalClient):  # type: ignore[misc,valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs.setdefault("connection_verify", False)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(saver_mod, "CosmosClient", _NoVerifyClient)
