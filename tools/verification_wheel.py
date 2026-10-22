from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
import sys
import sysconfig
import tempfile

from tools.verification_evidence import Summary, publish, save
from tools.verification_lanes import fresh
from tools.verification_process import run


def main() -> int:
    directory = Path("test-results/wheel-smoke").resolve()
    fresh(directory)
    code, digest, reason = 1, "", ""
    try:
        with tempfile.TemporaryDirectory(prefix="lg-verify-wheel-") as temporary:
            work = Path(temporary)
            code = run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(work / "dist")],
                directory / "logs/build.log",
            )
            wheels = list((work / "dist").glob("*.whl"))
            if code or len(wheels) != 1:
                raise RuntimeError("Expected one freshly built candidate wheel")
            digest = sha256(wheels[0].read_bytes()).hexdigest()
            code = run(
                [sys.executable, "-m", "venv", str(work / "venv")], directory / "logs/venv.log"
            )
            if code:
                raise RuntimeError("Wheel environment creation failed")
            python = str(work / "venv/bin/python")
            packages = (
                work
                / f"venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            )
            (packages / "verification-dependencies.pth").write_text(
                sysconfig.get_path("purelib") + "\n"
            )
            code = run(
                [python, "-m", "pip", "install", "--no-deps", "--no-index", str(wheels[0])],
                directory / "logs/install.log",
            )
            if code:
                raise RuntimeError("Wheel installation failed")
            script = (
                "from pathlib import Path; from importlib.metadata import version; "
                "import azure_functions_langgraph as a; "
                f"assert Path(a.__file__).resolve().is_relative_to(Path({str(work)!r})); "
                "assert version('azure-functions-langgraph') == "
                f"{version('azure-functions-langgraph')!r}; "
                "from langgraph.graph import StateGraph, START, END; from typing import TypedDict; "
                "S = TypedDict('S', {'value': str}); b = StateGraph(S); "
                "b.add_node('answer', lambda s: {'value': s['value'] + '-ok'}); "
                "b.add_edge(START, 'answer'); b.add_edge('answer', END); "
                "g = b.compile(); app = a.LangGraphApp(); app.register(graph=g, name='wheel'); "
                "assert g.invoke({'value': 'wheel'}) == {'value': 'wheel-ok'}; "
                "print('Installed candidate identity and graph round trip passed')"
            )
            code = run([python, "-I", "-c", script], directory / "logs/identity.log", cwd=work)
    except (OSError, RuntimeError) as error:
        code, reason = code or 1, str(error)
    summary = Summary(
        1,
        "wheel-smoke",
        ("installed-wheel", "graph-round-trip"),
        1,
        int(code == 0),
        int(code != 0),
        0,
        "PASS" if code == 0 else "FAIL",
        code,
    )
    publish(
        save(
            directory,
            replace(
                summary,
                artifact_sha256=digest,
                reason=reason,
                candidate_version=version("azure-functions-langgraph"),
            ),
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
