from __future__ import annotations

import importlib
import sqlite3
import sys
import types
from typing import Any, cast

pytest = cast(Any, importlib.import_module("pytest"))


def _install_fake_postgres(
    monkeypatch: Any,
    *,
    saver_setup_calls: list[int] | None = None,
    connect_calls: list[dict[str, Any]] | None = None,
    omit_postgres_saver: bool = False,
    omit_psycopg_connection: bool = False,
    omit_dict_row: bool = False,
) -> None:
    captured_setup = saver_setup_calls if saver_setup_calls is not None else []
    captured_connect = connect_calls if connect_calls is not None else []

    class FakeConnection:
        @classmethod
        def connect(cls, conn_string: str, **kwargs: Any) -> Any:
            captured_connect.append({"conn_string": conn_string, **kwargs})
            return cls()

    class FakePostgresSaver:
        def __init__(self, conn: Any) -> None:
            self.conn = conn
            self.setup_calls = 0

        def setup(self) -> None:
            self.setup_calls += 1
            captured_setup.append(self.setup_calls)

    postgres_module = types.ModuleType("langgraph.checkpoint.postgres")
    if not omit_postgres_saver:
        setattr(postgres_module, "PostgresSaver", FakePostgresSaver)

    psycopg_module = types.ModuleType("psycopg")
    if not omit_psycopg_connection:
        setattr(psycopg_module, "Connection", FakeConnection)

    rows_module = types.ModuleType("psycopg.rows")
    if not omit_dict_row:
        setattr(rows_module, "dict_row", object())

    monkeypatch.setitem(sys.modules, "langgraph", types.ModuleType("langgraph"))
    monkeypatch.setitem(
        sys.modules, "langgraph.checkpoint", types.ModuleType("langgraph.checkpoint")
    )
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", postgres_module)
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_module)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_module)


def _install_fake_sqlite(
    monkeypatch: Any,
    *,
    omit_sqlite_saver: bool = False,
) -> list[Any]:
    constructed: list[Any] = []

    class FakeSqliteSaver:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn
            self.setup_calls = 0
            constructed.append(self)

        def setup(self) -> None:
            self.setup_calls += 1

    sqlite_module = types.ModuleType("langgraph.checkpoint.sqlite")
    if not omit_sqlite_saver:
        setattr(sqlite_module, "SqliteSaver", FakeSqliteSaver)

    monkeypatch.setitem(sys.modules, "langgraph", types.ModuleType("langgraph"))
    monkeypatch.setitem(
        sys.modules, "langgraph.checkpoint", types.ModuleType("langgraph.checkpoint")
    )
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", sqlite_module)
    return constructed


def test_postgres_helper_opens_connection_and_runs_setup(monkeypatch: Any) -> None:
    setup_calls: list[int] = []
    connect_calls: list[dict[str, Any]] = []
    _install_fake_postgres(
        monkeypatch,
        saver_setup_calls=setup_calls,
        connect_calls=connect_calls,
    )

    module = importlib.import_module("azure_functions_langgraph.checkpointers.postgres")
    importlib.reload(module)
    saver = module.create_postgres_checkpointer("postgresql://u:p@h/db")

    assert setup_calls == [1]
    assert len(connect_calls) == 1
    assert connect_calls[0]["conn_string"] == "postgresql://u:p@h/db"
    assert connect_calls[0]["autocommit"] is True
    assert connect_calls[0]["prepare_threshold"] == 0
    assert "row_factory" in connect_calls[0]
    assert type(saver).__name__ == "FakePostgresSaver"
    assert hasattr(saver, "conn")
    assert saver.conn is not None


def test_postgres_helper_setup_false_skips_setup(monkeypatch: Any) -> None:
    setup_calls: list[int] = []
    _install_fake_postgres(monkeypatch, saver_setup_calls=setup_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.postgres")
    importlib.reload(module)
    module.create_postgres_checkpointer("postgresql://u:p@h/db", setup=False)

    assert setup_calls == []


def test_postgres_helper_forwards_autocommit_and_prepare_threshold(monkeypatch: Any) -> None:
    connect_calls: list[dict[str, Any]] = []
    _install_fake_postgres(monkeypatch, connect_calls=connect_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.postgres")
    importlib.reload(module)
    module.create_postgres_checkpointer(
        "postgresql://u:p@h/db",
        autocommit=False,
        prepare_threshold=None,
    )

    assert connect_calls[0]["autocommit"] is False
    assert "prepare_threshold" not in connect_calls[0]


def test_postgres_helper_missing_langgraph_postgres_raises_helpful_error(monkeypatch: Any) -> None:
    module = importlib.import_module("azure_functions_langgraph.checkpointers.postgres")
    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "langgraph.checkpoint.postgres":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match=r"\[postgres\]"):
        module.create_postgres_checkpointer("postgresql://u:p@h/db")


def test_postgres_helper_missing_psycopg_raises_helpful_error(monkeypatch: Any) -> None:
    _install_fake_postgres(monkeypatch)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.postgres")
    importlib.reload(module)

    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "psycopg":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match=r"\[postgres\]"):
        module.create_postgres_checkpointer("postgresql://u:p@h/db")


def test_postgres_helper_missing_psycopg_rows_module_raises(monkeypatch: Any) -> None:
    _install_fake_postgres(monkeypatch)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.postgres")
    importlib.reload(module)

    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "psycopg.rows":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match=r"\[postgres\]"):
        module.create_postgres_checkpointer("postgresql://u:p@h/db")


def test_postgres_helper_missing_postgres_saver_symbol_raises(monkeypatch: Any) -> None:
    _install_fake_postgres(monkeypatch, omit_postgres_saver=True)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.postgres")
    importlib.reload(module)

    with pytest.raises(ImportError, match="PostgresSaver"):
        module.create_postgres_checkpointer("postgresql://u:p@h/db")


def test_postgres_helper_missing_psycopg_connection_symbol_raises(monkeypatch: Any) -> None:
    _install_fake_postgres(monkeypatch, omit_psycopg_connection=True)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.postgres")
    importlib.reload(module)

    with pytest.raises(ImportError, match="Connection"):
        module.create_postgres_checkpointer("postgresql://u:p@h/db")


def test_postgres_helper_missing_dict_row_symbol_raises(monkeypatch: Any) -> None:
    _install_fake_postgres(monkeypatch, omit_dict_row=True)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.postgres")
    importlib.reload(module)

    with pytest.raises(ImportError, match="dict_row"):
        module.create_postgres_checkpointer("postgresql://u:p@h/db")


def test_sqlite_helper_opens_connection_and_runs_setup(monkeypatch: Any) -> None:
    constructed = _install_fake_sqlite(monkeypatch)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.sqlite")
    importlib.reload(module)
    saver = module.create_sqlite_checkpointer(":memory:")

    assert constructed == [saver]
    assert getattr(saver, "setup_calls") == 1
    assert isinstance(saver.conn, sqlite3.Connection)


def test_sqlite_helper_setup_false_skips_setup(monkeypatch: Any) -> None:
    _install_fake_sqlite(monkeypatch)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.sqlite")
    importlib.reload(module)
    saver = module.create_sqlite_checkpointer(":memory:", setup=False)

    assert getattr(saver, "setup_calls") == 0


def test_sqlite_helper_check_same_thread_default_is_false(monkeypatch: Any) -> None:
    _install_fake_sqlite(monkeypatch)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.sqlite")
    importlib.reload(module)

    captured: dict[str, Any] = {}
    real_connect = sqlite3.connect

    def spy_connect(database: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_connect(database, **kwargs)

    monkeypatch.setattr(module.importlib.import_module("sqlite3"), "connect", spy_connect)
    module.create_sqlite_checkpointer(":memory:")

    assert captured["check_same_thread"] is False


def test_sqlite_helper_missing_langgraph_sqlite_raises_helpful_error(monkeypatch: Any) -> None:
    module = importlib.import_module("azure_functions_langgraph.checkpointers.sqlite")
    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "langgraph.checkpoint.sqlite":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match=r"\[sqlite\]"):
        module.create_sqlite_checkpointer(":memory:")


def test_sqlite_helper_missing_sqlite_saver_symbol_raises(monkeypatch: Any) -> None:
    _install_fake_sqlite(monkeypatch, omit_sqlite_saver=True)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.sqlite")
    importlib.reload(module)

    with pytest.raises(ImportError, match="SqliteSaver"):
        module.create_sqlite_checkpointer(":memory:")


def test_checkpointers_package_exports_helpers() -> None:
    pkg = importlib.import_module("azure_functions_langgraph.checkpointers")
    assert "create_postgres_checkpointer" in pkg.__all__
    assert "create_sqlite_checkpointer" in pkg.__all__
    assert callable(pkg.create_postgres_checkpointer)
    assert callable(pkg.create_sqlite_checkpointer)
