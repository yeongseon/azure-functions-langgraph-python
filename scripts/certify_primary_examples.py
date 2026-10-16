from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Final, Literal

PRIMARY_EXAMPLES: Final = (
    "azure_openai_agent",
    "conversation_memory",
    "tool_calling_agent",
    "production_persistent_agent",
)
ADAPTER_REQUIREMENT: Final = re.compile(
    r"^\s*azure-functions-langgraph(?P<extras>\[[^]]+])?\s*(?:[<>=!~].*)?$",
    re.IGNORECASE,
)
GRAPH_NAMES: Final = {
    "azure_openai_agent": "azure_openai_agent",
    "conversation_memory": "conversation_agent",
    "tool_calling_agent": "tool_calling_agent",
    "production_persistent_agent": "production_persistent_agent",
}


@dataclass(frozen=True, slots=True)
class CertificationConfig:
    lane: Literal["published", "candidate"]
    wheel: Path | None
    output_dir: Path
    python: str
    uv: str


def adapter_free_requirements(requirements: Path) -> str:
    lines = requirements.read_text().splitlines(keepends=True)
    return "".join(line for line in lines if ADAPTER_REQUIREMENT.fullmatch(line.strip()) is None)


def _adapter_extras(requirements: Path) -> str:
    for line in requirements.read_text().splitlines():
        match = ADAPTER_REQUIREMENT.fullmatch(line.strip())
        if match is not None:
            return match.group("extras") or ""
    raise RuntimeError(f"missing azure-functions-langgraph requirement: {requirements}")


def parse_args(argv: list[str] | None = None) -> CertificationConfig:
    parser = argparse.ArgumentParser(
        description="Certify clean installs of the four primary examples."
    )
    parser.add_argument("lane", choices=("published", "candidate"))
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--python", default="3.11")
    parser.add_argument("--uv", default=os.environ.get("UV", "uv"))
    args = parser.parse_args(argv)
    lane: Literal["published", "candidate"] = args.lane
    if lane == "published" and args.wheel is not None:
        parser.error("the published lane installs requirements unchanged")
    if lane == "candidate" and args.wheel is None:
        parser.error("the candidate lane requires --wheel")
    output_dir = args.output_dir or Path(tempfile.mkdtemp(prefix=f"primary-examples-{lane}-"))
    return CertificationConfig(
        lane=lane,
        wheel=args.wheel,
        output_dir=output_dir,
        python=args.python,
        uv=args.uv,
    )


def _run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{output}")
    return output


def _venv_env(venv: Path) -> dict[str, str]:
    return {**os.environ, "VIRTUAL_ENV": str(venv), "UV_LINK_MODE": "copy"}


def _install(config: CertificationConfig, requirements: Path, venv: Path) -> None:
    _run([config.uv, "venv", "--python", config.python, str(venv)])
    env = _venv_env(venv)
    match config.lane:
        case "published":
            _run([config.uv, "pip", "install", "-r", str(requirements)], env=env)
        case "candidate":
            if config.wheel is None:
                raise RuntimeError("candidate wheel was not parsed")
            generated = venv.parent / "requirements-without-adapter.txt"
            generated.write_text(adapter_free_requirements(requirements))
            wheel_requirement = (
                f"azure-functions-langgraph{_adapter_extras(requirements)} "
                f"@ {config.wheel.resolve().as_uri()}"
            )
            _run(
                [config.uv, "pip", "install", "-r", str(generated), wheel_requirement],
                env=env,
            )
    _run([config.uv, "pip", "check"], env=env)


def _smoke(example: str, venv: Path, repo: Path, lane: str) -> tuple[str, str]:
    python = venv / "bin" / "python"
    example_dir = repo / "examples" / example
    fake_env = {
        **os.environ,
        "AGENT_MODEL_MODE": "fake",
        "AZURE_STORAGE_CONNECTION_STRING": (
            "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
            "AccountKey=Eby8vdM02xNOcqFeqCnf2w==;BlobEndpoint=http://127.0.0.1:10000/"
            "devstoreaccount1;TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
        ),
        "LANGGRAPH_AUTO_CREATE_STORAGE": "false",
    }
    fake_code = (
        "from pathlib import Path\n"
        "import azure_functions_langgraph\n"
        "import function_app\n"
        "metadata = function_app.langgraph_app.get_app_metadata().graphs"
        f"[{GRAPH_NAMES[example]!r}]\n"
        "assert any(route.method == 'POST' and route.path.endswith('/invoke') "
        "for route in metadata.routes)\n"
        "module_path = Path(azure_functions_langgraph.__file__).resolve()\n"
        f"repo = Path({str(repo)!r}).resolve()\n"
        f"assert {lane!r} != 'candidate' or not module_path.is_relative_to(repo)\n"
        "print(f'adapter={module_path}')\n"
        "print(f'routes={[route.path for route in metadata.routes]}')\n"
    )
    fake_output = _run([str(python), "-c", fake_code], env=fake_env, cwd=example_dir)

    real_env = {
        **os.environ,
        "AGENT_MODEL_MODE": "azure_openai",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
        "AZURE_OPENAI_DEPLOYMENT": "certification-deployment",
        "AZURE_OPENAI_API_KEY": "not-a-real-key",
        "AZURE_OPENAI_USE_ENTRA_ID": "false",
    }
    real_code = (
        "from unittest.mock import patch\n"
        "with patch('langchain_openai.AzureChatOpenAI') as client:\n"
        "    client.return_value.bind_tools.return_value = client.return_value\n"
        "    import graph\n"
        "    assert client.call_count == 1\n"
        "    kwargs = client.call_args.kwargs\n"
        "    assert kwargs['azure_deployment'] == 'certification-deployment'\n"
        "    print(f'mocked AzureChatOpenAI kwargs={sorted(kwargs)}')\n"
    )
    real_output = _run([str(python), "-c", real_code], env=real_env, cwd=example_dir)
    return fake_output, real_output


def _certify_negative_fixture(config: CertificationConfig, repo: Path) -> None:
    venv = config.output_dir / "negative-venv"
    _run([config.uv, "venv", "--python", config.python, str(venv)])
    result = subprocess.run(
        [
            config.uv,
            "pip",
            "install",
            "-r",
            str(repo / "tests/fixtures/example_certification/incompatible/requirements.txt"),
        ],
        env=_venv_env(venv),
        capture_output=True,
        text=True,
        check=False,
    )
    (config.output_dir / "negative-resolution.txt").write_text(result.stdout + result.stderr)
    if result.returncode == 0:
        raise RuntimeError("deliberately incompatible dependency fixture unexpectedly resolved")


def certify(config: CertificationConfig) -> None:
    repo = Path(__file__).resolve().parents[1]
    config.output_dir.mkdir(parents=True, exist_ok=True)
    _certify_negative_fixture(config, repo)
    summary: dict[str, dict[str, str]] = {}
    for example in PRIMARY_EXAMPLES:
        example_output = config.output_dir / example
        venv = example_output / "venv"
        if example_output.exists():
            shutil.rmtree(example_output)
        example_output.mkdir(parents=True)
        requirements = repo / "examples" / example / "requirements.txt"
        _install(config, requirements, venv)
        env = _venv_env(venv)
        versions = _run([config.uv, "pip", "freeze"], env=env)
        fake_output, real_output = _smoke(example, venv, repo, config.lane)
        (example_output / "installed-versions.txt").write_text(versions)
        (example_output / "fake-route-smoke.txt").write_text(fake_output)
        (example_output / "real-model-construction.txt").write_text(real_output)
        summary[example] = {
            "requirements": str(requirements.relative_to(repo)),
            "versions": f"{example}/installed-versions.txt",
            "fake_smoke": f"{example}/fake-route-smoke.txt",
            "real_model": f"{example}/real-model-construction.txt",
        }
        print(f"certified {config.lane}: {example}")
    (config.output_dir / "summary.json").write_text(
        json.dumps({"lane": config.lane, "python": config.python, "examples": summary}, indent=2)
        + "\n"
    )
    print(f"certification artifacts: {config.output_dir}")


if __name__ == "__main__":
    certify(parse_args())
