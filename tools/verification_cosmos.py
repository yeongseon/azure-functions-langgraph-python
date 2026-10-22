from __future__ import annotations

import os
from pathlib import Path

from tools.verification_process import run
from tools.verification_services import MissingPrerequisite


def trust_certificate(temporary: Path, directory: Path) -> None:
    endpoint = "https://localhost:" + os.environ.get("COSMOS_PORT", "8081")
    certificate = temporary / "cosmos-emulator.pem"
    code = run(
        [
            "curl",
            "--fail",
            "--silent",
            "--insecure",
            "--max-time",
            "15",
            endpoint + "/_explorer/emulator.pem",
            "--output",
            str(certificate),
        ],
        directory / "logs/certificate.log",
        20,
    )
    if code:
        raise MissingPrerequisite("Unable to fetch the run-owned emulator certificate")
    os.environ["REQUESTS_CA_BUNDLE"] = str(certificate)
    os.environ["SSL_CERT_FILE"] = str(certificate)
    os.environ["COSMOS_EMULATOR_ENDPOINT"] = endpoint
