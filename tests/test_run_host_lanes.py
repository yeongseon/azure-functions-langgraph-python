"""Offline tests of the local host-lane runner (#880 #882 #883).

``tools/run_host_lanes.py`` starts real Functions hosts and Azurite, which these tests never do:
``child`` is replaced by a recording fake, so they prove the runner's own logic (what it starts and
with which settings, what it accepts as proof, and what it refuses) without a host, an emulator or
a network. The lanes themselves run through ``make host-lanes``.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
from typing import Any, NamedTuple

import pytest
import requests

from azure_functions_langgraph._topology import LOCK_BACKEND_ENV, THREAD_STORE_ENV
from tests.topology_truth_table import RULE_PATTERNS

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("host_lanes_runner", ROOT / "tools/run_host_lanes.py")
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

PORTS = {"blob": 11000, "queue": 11001, "table": 11002}
INDEXING_LOG = "Worker failed to index functions\nValueError: {expected}\n"


class FakeProcess:
    def __init__(self, code: int | None = None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


class Countdown:
    """Reports "still running" a fixed number of times, then exits with a status."""

    def __init__(self, polls_before_exit: int, code: int) -> None:
        self.remaining = polls_before_exit
        self.code = code

    def poll(self) -> int | None:
        if self.remaining > 0:
            self.remaining -= 1
            return None
        return self.code


class Started(NamedTuple):
    command: list[str]
    log: Path
    cwd: Path


def install_child(
    monkeypatch: pytest.MonkeyPatch,
    *,
    process: Any = None,
    on_start: Callable[[Started], object] | None = None,
) -> list[Started]:
    """Replace ``child`` with a fake that records each start and runs ``on_start`` for it."""
    started: list[Started] = []

    @contextmanager
    def fake_child(command: list[str], log: Path, cwd: Path) -> Iterator[Any]:
        record = Started(command, log, cwd)
        started.append(record)
        log.write_text("")
        if on_start is not None:
            on_start(record)
        yield process if process is not None else FakeProcess(0)

    monkeypatch.setattr(runner, "child", fake_child)
    return started


def junit(classname: str, *, count: int = 2, outcome: str = "", skipped: int = 0) -> str:
    cases = "".join(
        f'<testcase classname="{classname}" name="test_{number}">{outcome}</testcase>'
        for number in range(count)
    )
    suite = f'tests="{count}" failures="0" errors="0" skipped="{skipped}"'
    return f"<testsuites><testsuite {suite}>{cases}</testsuite></testsuites>"


def report_path(command: list[str]) -> Path:
    return Path(next(item for item in command if item.startswith("--junitxml=")).split("=", 1)[1])


def settings_of(app: Path) -> dict[str, str]:
    values: dict[str, str] = json.loads((app / "local.settings.json").read_text())["Values"]
    return values


@pytest.fixture
def signal_handlers() -> Iterator[None]:
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for number, handler in previous.items():
        signal.signal(number, handler)


def test_connection_string_points_every_service_at_its_loopback_port() -> None:
    text = runner.connection_string(PORTS)

    assert "AccountName=devstoreaccount1" in text and runner.STORAGE_KEY in text
    for service, port in PORTS.items():
        assert f"{service.title()}Endpoint=http://127.0.0.1:{port}/devstoreaccount1" in text


def test_azurite_runs_silent_with_the_api_version_check_off_on_loopback(tmp_path: Path) -> None:
    command = runner.azurite_command(tmp_path / "data", PORTS)

    assert command[:5] == [
        "azurite",
        "--silent",
        "--skipApiVersionCheck",
        "--location",
        str(tmp_path / "data"),
    ]
    for service, port in PORTS.items():
        at = command.index(f"--{service}Host")
        assert command[at : at + 4] == [
            f"--{service}Host",
            "127.0.0.1",
            f"--{service}Port",
            str(port),
        ]


def test_the_host_command_serves_the_python_app_on_the_given_port() -> None:
    assert runner.host_command(7123) == ["func", "start", "--python", "--port", "7123"]


def test_write_app_copies_the_python_files_and_host_json_only(tmp_path: Path) -> None:
    # Given: a source directory with app files and files a host must not see
    source = tmp_path / "source"
    (source / "wheels").mkdir(parents=True)
    for name in ("function_app.py", "__init__.py", "host.json", "requirements.txt", "README.md"):
        (source / name).write_text("{}" if name == "host.json" else "x = 1\n")

    # When: the app is written
    app = runner.write_app(tmp_path / "app", source, connection="CONN", settings={"A": "1"})

    # Then: only what the host runs is copied, with the settings the lane asked for
    assert sorted(path.name for path in app.iterdir()) == [
        "function_app.py",
        "host.json",
        "local.settings.json",
    ]
    values = settings_of(app)
    assert values["AzureWebJobsStorage"] == "CONN" and values["A"] == "1"
    assert values["FUNCTIONS_WORKER_RUNTIME"] == "python"


def test_a_lane_setting_overrides_a_host_default(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    app = runner.write_app(
        tmp_path / "app", source, connection="C", settings={"PYTHON_ENABLE_INIT_INDEXING": "0"}
    )

    assert settings_of(app)["PYTHON_ENABLE_INIT_INDEXING"] == "0"


def test_the_platform_app_is_copied_exactly(tmp_path: Path) -> None:
    app = runner.write_app(tmp_path / "app", runner.PLATFORM_APP, connection="C", settings={})

    assert {path.name for path in app.iterdir()} == {
        "function_app.py",
        "host.json",
        "local.settings.json",
    }


def test_the_e2e_app_is_copied_with_its_graphs_and_without_packaging_files(tmp_path: Path) -> None:
    app = runner.write_app(tmp_path / "app", runner.E2E_APP, connection="C", settings={})

    copied = {path.name for path in app.iterdir()}
    assert {"function_app.py", "graph.py", "lock_graph.py", "host.json"} <= copied
    assert not copied & {"requirements.txt", "README.md", "wheels"}


def test_environment_sets_values_and_puts_every_variable_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANE_TEST_KEPT", "before")
    monkeypatch.delenv("LANE_TEST_NEW", raising=False)

    with runner.environment(LANE_TEST_KEPT="during", LANE_TEST_NEW="added"):
        assert (os.environ["LANE_TEST_KEPT"], os.environ["LANE_TEST_NEW"]) == ("during", "added")

    assert os.environ["LANE_TEST_KEPT"] == "before"
    assert "LANE_TEST_NEW" not in os.environ


def test_environment_is_restored_when_the_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANE_TEST_NEW", raising=False)

    with pytest.raises(RuntimeError, match="boom"):
        with runner.environment(LANE_TEST_NEW="added"):
            raise RuntimeError("boom")

    assert "LANE_TEST_NEW" not in os.environ


def test_wait_for_exit_returns_the_status_once_the_process_exits() -> None:
    assert runner.wait_for_exit(Countdown(2, 3), 5, "pytest", interval=0) == 3


def test_wait_for_exit_raises_instead_of_blocking_on_a_hung_process() -> None:
    with pytest.raises(RuntimeError, match="locks: pytest did not exit within 0s"):
        runner.wait_for_exit(FakeProcess(None), 0, "locks: pytest", interval=0)


def test_wait_port_returns_once_something_listens_and_raises_when_nothing_does() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        runner.wait_port(listener.getsockname()[1], timeout=2)
        closed = listener.getsockname()[1]

    with pytest.raises(RuntimeError, match=f"Azurite did not open port {closed}"):
        runner.wait_port(closed, timeout=0.3)


def test_tail_keeps_only_the_last_lines_of_a_log(tmp_path: Path) -> None:
    log = tmp_path / "host.log"
    log.write_text("\n".join(f"line {number}" for number in range(100)))

    assert runner.tail(log, 3) == "line 97\nline 98\nline 99"


class TestRunPytest:
    AREAS = [("platform sdk", "test_platform_sdk")]

    def run(self, tmp_path: Path, env: dict[str, str] | None = None) -> None:
        runner.run_pytest(
            tmp_path, "platform", "tests/host_lanes/test_platform_sdk.py", self.AREAS, env or {}
        )

    def write_report(self, xml: str | None, log_text: str = "") -> Callable[[Started], None]:
        def on_start(started: Started) -> None:
            started.log.write_text(log_text)
            if xml is not None:
                report_path(started.command).write_text(xml)

        return on_start

    def test_a_complete_report_passes_and_is_counted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Given: pytest exits 0 and its report holds the area's tests, none skipped
        seen: dict[str, str | None] = {}

        def on_start(started: Started) -> None:
            seen["during"] = os.environ.get("LANE_PLATFORM_BASE_URL")
            report_path(started.command).write_text(junit("tests.host_lanes.test_platform_sdk"))

        started = install_child(monkeypatch, on_start=on_start)
        monkeypatch.delenv("LANE_PLATFORM_BASE_URL", raising=False)

        # When: the lane runs the module
        self.run(tmp_path, {"LANE_PLATFORM_BASE_URL": "http://127.0.0.1:1"})

        # Then: the e2e module ran selected, uncovered and from the checkout, with its input set
        command = started[0].command
        assert command[1:5] == ["-m", "pytest", "-m", "e2e"]
        assert command[-1].endswith("test_platform_sdk.py")
        assert {"--no-cov", "no:cacheprovider"} <= set(command)
        assert started[0].cwd == runner.ROOT
        assert seen["during"] == "http://127.0.0.1:1"
        assert "LANE_PLATFORM_BASE_URL" not in os.environ
        assert "platform: 2 tests ran, none failed, errored or skipped" in capsys.readouterr().out

    def test_a_failing_run_raises_with_the_log_tail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_child(
            monkeypatch,
            process=FakeProcess(1),
            on_start=self.write_report(junit("tests.host_lanes.test_platform_sdk"), "FAILED boom"),
        )

        with pytest.raises(RuntimeError, match="platform: pytest exited with status 1") as caught:
            self.run(tmp_path)

        assert "FAILED boom" in str(caught.value)

    def test_a_report_with_a_skip_does_not_count_even_though_pytest_exited_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        skipped = junit("tests.host_lanes.test_platform_sdk", outcome="<skipped/>", skipped=2)
        install_child(monkeypatch, on_start=self.write_report(skipped))

        with pytest.raises(RuntimeError, match="does not prove the lane ran.*skipped"):
            self.run(tmp_path)

    def test_a_report_without_the_required_area_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_child(monkeypatch, on_start=self.write_report(junit("tests.test_other")))

        with pytest.raises(RuntimeError, match="missing expected integration areas"):
            self.run(tmp_path)

    def test_a_missing_report_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_child(monkeypatch, on_start=self.write_report(None))

        with pytest.raises(RuntimeError, match="does not prove the lane ran"):
            self.run(tmp_path)

    def test_a_pytest_that_never_exits_raises_and_restores_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_child(monkeypatch, process=FakeProcess(None))
        monkeypatch.setattr(runner, "PYTEST_TIMEOUT", 0)
        monkeypatch.delenv("LANE_PLATFORM_BASE_URL", raising=False)

        with pytest.raises(RuntimeError, match="platform: pytest did not exit within 0s"):
            self.run(tmp_path, {"LANE_PLATFORM_BASE_URL": "http://127.0.0.1:1"})

        assert "LANE_PLATFORM_BASE_URL" not in os.environ


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.parametrize(
    ("status", "served"), [(200, True), (204, True), (404, False), (500, False)]
)
def test_a_route_is_served_only_below_400(
    monkeypatch: pytest.MonkeyPatch, status: int, served: bool
) -> None:
    monkeypatch.setattr(runner.requests, "request", lambda *a, **k: FakeResponse(status))

    assert runner.answers("GET", "http://127.0.0.1:1/api/health") is served


def test_no_answer_at_all_is_not_served(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> FakeResponse:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(runner.requests, "request", refuse)

    assert runner.answers("GET", "http://127.0.0.1:1/api/health") is False


def test_posts_carry_an_empty_json_body_and_gets_carry_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, Any]] = []

    def record(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append((method, url, kwargs["json"]))
        return FakeResponse(404)

    monkeypatch.setattr(runner.requests, "request", record)

    assert runner.serves_any_route("http://127.0.0.1:9") is False
    assert calls == [
        ("GET", "http://127.0.0.1:9/api/health", None),
        ("POST", "http://127.0.0.1:9/api/threads", {}),
    ]


def test_one_served_route_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner.requests,
        "request",
        lambda method, url, **k: FakeResponse(200 if url.endswith("/api/threads") else 404),
    )

    assert runner.serves_any_route("http://127.0.0.1:9") is True


REJECTION = runner.REJECTIONS[0]


def refuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    log_text: str,
    *,
    served: bool = False,
    host: Any = None,
) -> None:
    """Run the refusal check against a fake host whose log holds ``log_text``."""
    install_child(
        monkeypatch,
        process=host if host is not None else FakeProcess(),
        on_start=lambda started: started.log.write_text(log_text),
    )
    monkeypatch.setattr(runner, "serves_any_route", lambda url: served)
    monkeypatch.setattr(runner, "REJECTION_TIMEOUT", 0.05)
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    runner.expect_rejection(
        REJECTION, ["func", "start"], tmp_path, tmp_path / "host.log", "http://127.0.0.1:1"
    )


def test_a_host_that_refuses_the_topology_and_serves_nothing_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    refuse(monkeypatch, tmp_path, INDEXING_LOG.format(expected=REJECTION.expected))


def test_a_host_that_logs_no_indexing_failure_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="in-process lock: the host logged no indexing"):
        refuse(monkeypatch, tmp_path, "Host started\n")


def test_a_refusal_without_the_package_diagnostic_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="refused it without logging"):
        refuse(monkeypatch, tmp_path, INDEXING_LOG.format(expected="something else"))


def test_a_refused_topology_that_still_serves_a_route_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = INDEXING_LOG.format(expected=REJECTION.expected)

    with pytest.raises(RuntimeError, match="still serves a route"):
        refuse(monkeypatch, tmp_path, log, served=True)


def test_a_host_that_exits_without_a_diagnostic_fails_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="logged no indexing failure"):
        refuse(monkeypatch, tmp_path, "", host=FakeProcess(1))


def test_an_accepted_topology_fails_at_once_without_waiting_for_the_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a host that already answers its routes, a long timeout, and no sleeping allowed
    def no_sleep(seconds: float) -> None:
        raise AssertionError(f"waited {seconds}s for a host that was already serving")

    monkeypatch.setattr(runner.time, "sleep", no_sleep)
    monkeypatch.setattr(runner, "REJECTION_TIMEOUT", 600)

    # When / Then: the refusal check gives up on its first poll
    with pytest.raises(RuntimeError, match="logged no indexing failure"):
        refuse(monkeypatch, tmp_path, "", served=True)


def fake_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    numbers = iter(range(12000, 12100))
    monkeypatch.setattr(runner, "available_port", lambda: next(numbers))


def test_the_topology_lane_refuses_each_rejection_on_an_app_declared_distributed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: every refusal check recorded instead of run
    fake_ports(monkeypatch)
    calls: list[tuple[Any, list[str], Path, Path, str]] = []
    monkeypatch.setattr(runner, "expect_rejection", lambda *args: calls.append(args))

    # When: the lane runs
    runner.lane_topology(tmp_path, "CONN")

    # Then: each rejection gets its own app, declared distributed with exactly one piece replaced
    assert [call[0] for call in calls] == list(runner.REJECTIONS)
    for index, (rejection, command, app, log, url) in enumerate(calls):
        values = settings_of(app)
        assert {name: values[name] for name in runner.DISTRIBUTED} == runner.DISTRIBUTED
        assert {name: values[name] for name in rejection.settings} == rejection.settings
        assert values["AzureWebJobsStorage"] == "CONN"
        assert command == runner.host_command(int(url.rsplit(":", 1)[1]))
        assert log == tmp_path / f"topology-{index}.log"


def test_the_platform_lane_runs_the_sdk_module_against_one_distributed_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a recorded host, readiness check, pytest run and log check
    fake_ports(monkeypatch)
    events: list[Any] = []
    started = install_child(monkeypatch, on_start=lambda s: events.append("host started"))
    monkeypatch.setattr(runner, "wait_ready", lambda host, url: events.append(("ready", url)))
    monkeypatch.setattr(runner, "run_pytest", lambda *args: events.append(("pytest", args[1:])))
    monkeypatch.setattr(runner, "check_host_log", lambda name, log: events.append(("log", name)))

    # When: the lane runs
    runner.lane_platform(tmp_path, "CONN")

    # Then: one host declared distributed is waited for, tested, stopped and then its log checked
    url = "http://127.0.0.1:12000"
    assert events == [
        "host started",
        ("ready", url),
        (
            "pytest",
            (
                "platform",
                "tests/host_lanes/test_platform_sdk.py",
                [("platform sdk", "test_platform_sdk")],
                {"LANE_PLATFORM_BASE_URL": url},
            ),
        ),
        ("log", "platform"),
    ]
    assert started[0].command == runner.host_command(12000)
    values = settings_of(started[0].cwd)
    assert {name: values[name] for name in runner.DISTRIBUTED} == runner.DISTRIBUTED


class TestLocksLane:
    @pytest.fixture(autouse=True)
    def _ports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_ports(monkeypatch)
        monkeypatch.setattr(runner, "wait_ready", lambda host, url: None)

    def test_two_hosts_share_the_storage_and_the_e2e_gets_both_urls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[Any, ...]] = []
        started = install_child(monkeypatch)
        monkeypatch.setattr(runner, "run_pytest", lambda *args: calls.append(args))

        runner.lane_locks(tmp_path, "CONN")

        assert [s.command for s in started] == [
            runner.host_command(12000),
            runner.host_command(12001),
        ]
        for s in started:
            values = settings_of(s.cwd)
            assert values["AzureWebJobsStorage"] == "CONN"
            assert values["E2E_LOCK_HOLD_SECONDS"] == runner.LOCK_HOLD_SECONDS
        assert started[0].cwd != started[1].cwd
        assert calls == [
            (
                tmp_path,
                "locks",
                "tests/e2e/test_langgraph_lock_e2e.py",
                [("locks", "test_langgraph_lock_e2e")],
                {
                    "E2E_BASE_URL": "http://127.0.0.1:12000",
                    "E2E_BASE_URL_B": "http://127.0.0.1:12001",
                },
            )
        ]

    def test_a_swallowed_lock_wiring_failure_fails_the_lane(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Given: the e2e app logs the failure it otherwise survives (it keeps serving e2e_agent)
        def on_start(started: Started) -> None:
            if started.cwd.name == "locks-b":
                started.log.write_text("e2e_lock_agent wiring failed; continuing with e2e_agent")

        install_child(monkeypatch, on_start=on_start)
        monkeypatch.setattr(runner, "run_pytest", lambda *args: None)

        # When / Then: the lane names the host
        with pytest.raises(RuntimeError, match="locks-b: the e2e app logged a swallowed lock"):
            runner.lane_locks(tmp_path, "CONN")

    def test_a_startup_failure_in_a_host_log_fails_the_lane(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_child(
            monkeypatch,
            on_start=lambda started: started.log.write_text("Worker failed to start"),
        )
        monkeypatch.setattr(runner, "run_pytest", lambda *args: None)

        with pytest.raises(RuntimeError, match="locks-a host emitted a startup/indexing failure"):
            runner.lane_locks(tmp_path, "CONN")


def install_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def storage(temporary: Path) -> Iterator[str]:
        yield "CONN"

    monkeypatch.setattr(runner, "storage", storage)
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_main_names_a_missing_prerequisite_before_starting_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda name: None if name == "azurite" else "/x")

    def started(*args: Any) -> None:
        raise AssertionError("a resource was acquired without its prerequisite")

    monkeypatch.setattr(runner, "storage", started)

    assert runner.main(["--lane", "topology"]) == 1
    assert "Missing prerequisite: azurite" in capsys.readouterr().err


def test_main_refuses_a_lane_it_does_not_know() -> None:
    with pytest.raises(SystemExit) as caught:
        runner.main(["--lane", "nonexistent"])

    assert caught.value.code == 2


def test_main_runs_the_selected_lanes_in_order_and_removes_its_temporary_directory(
    monkeypatch: pytest.MonkeyPatch, signal_handlers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    install_storage(monkeypatch)
    seen: list[tuple[str, str, Path]] = []
    lanes = {
        name: (lambda temporary, connection, name=name: seen.append((name, connection, temporary)))
        for name in ("locks", "platform", "topology")
    }
    monkeypatch.setattr(runner, "LANES", lanes)

    assert runner.main(["--lane", "topology", "--lane", "platform"]) == 0

    assert [name for name, _, _ in seen] == ["topology", "platform"]
    assert {connection for _, connection, _ in seen} == {"CONN"}
    assert not seen[0][2].exists()
    out = capsys.readouterr().out
    assert "lane topology: passed" in out and "owned resources removed" in out


def test_main_runs_every_lane_by_default(
    monkeypatch: pytest.MonkeyPatch, signal_handlers: None
) -> None:
    install_storage(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(
        runner,
        "LANES",
        {name: (lambda t, c, name=name: seen.append(name)) for name in ("locks", "platform")},
    )

    assert runner.main([]) == 0

    assert seen == ["locks", "platform"]


def test_a_failing_lane_exits_1_with_its_message_and_the_logs_of_the_run(
    monkeypatch: pytest.MonkeyPatch, signal_handlers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    install_storage(monkeypatch)
    kept: list[Path] = []

    def failing(temporary: Path, connection: str) -> None:
        kept.append(temporary)
        (temporary / "host.log").write_text("first\nlast line of the host log")
        raise RuntimeError("the lane failed")

    monkeypatch.setattr(runner, "LANES", {"locks": failing})

    assert runner.main([]) == 1

    err = capsys.readouterr().err
    assert "error: the lane failed" in err
    assert "--- host.log (last lines) ---" in err and "last line of the host log" in err
    assert not kept[0].exists()


def test_an_interrupt_still_prints_the_logs_and_propagates(
    monkeypatch: pytest.MonkeyPatch, signal_handlers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    install_storage(monkeypatch)

    def interrupted(temporary: Path, connection: str) -> None:
        (temporary / "host.log").write_text("still running")
        raise KeyboardInterrupt("signal 15")

    monkeypatch.setattr(runner, "LANES", {"locks": interrupted})

    with pytest.raises(KeyboardInterrupt):
        runner.main([])

    assert "still running" in capsys.readouterr().err


def test_each_rejection_expects_the_message_of_the_truth_table_rule_it_names() -> None:
    for rejection in runner.REJECTIONS:
        matching = [
            rule for rule, pattern in RULE_PATTERNS.items() if pattern.search(rejection.expected)
        ]
        assert matching == [rejection.rule], rejection.label


def test_the_lane_refuses_every_declared_distributed_rule_the_truth_table_defines() -> None:
    declared = sorted(rule for rule in RULE_PATTERNS if rule.endswith("-declared"))

    assert sorted(rejection.rule for rejection in runner.REJECTIONS) == declared


def test_the_declaration_is_made_with_the_variables_the_package_reads() -> None:
    assert runner.DISTRIBUTED == {LOCK_BACKEND_ENV: "distributed", THREAD_STORE_ENV: "distributed"}


def test_every_rejection_setting_is_read_by_the_lane_app() -> None:
    source = (runner.PLATFORM_APP / "function_app.py").read_text()

    for rejection in runner.REJECTIONS:
        for name in rejection.settings:
            assert f'"{name}"' in source, name


def test_the_lock_is_held_long_enough_for_a_loaded_machine() -> None:
    assert float(runner.LOCK_HOLD_SECONDS) >= 8


def test_the_runner_starts_processes_only_through_the_pinned_child_helper() -> None:
    # security/bandit-policy.json pins subprocess findings by file and line, so a new import or
    # call here would be an unreviewed finding; run_host_smoke.child is the reviewed one.
    tree = ast.parse((ROOT / "tools" / "run_host_lanes.py").read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "subprocess" not in imported
