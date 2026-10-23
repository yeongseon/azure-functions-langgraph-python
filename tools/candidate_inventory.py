from __future__ import annotations

from dataclasses import dataclass
from email.parser import Parser
from importlib.metadata import distributions
from pathlib import Path
import re
from zipfile import ZipFile

from tools.security_report import EvidenceError, identifier, load, mapping, sequence


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    version: str

    @classmethod
    def from_wheel(cls, wheel: Path) -> Candidate:
        with ZipFile(wheel) as archive:
            paths = [p for p in archive.namelist() if p.endswith(".dist-info/METADATA")]
            if len(paths) != 1:
                raise EvidenceError("Candidate METADATA missing or ambiguous")
            metadata = Parser().parsestr(archive.read(paths[0]).decode("utf-8"))
        return cls(canonical(identifier(metadata["Name"])), identifier(metadata["Version"]))

    def freeze(self, target: Path) -> tuple[str, ...]:
        installed = [
            (canonical(identifier(dist.metadata["Name"])), identifier(dist.version))
            for dist in distributions(path=[str(target)])
        ]
        candidates = [(name, version) for name, version in installed if name == self.name]
        if candidates != [(self.name, self.version)]:
            raise EvidenceError("Installed candidate identity absent or mismatched")
        return tuple(
            sorted(f"{name}=={version}" for name, version in installed if name != self.name)
        )

    def verify_report(self, raw: str, requirements: tuple[str, ...]) -> None:
        observed: list[str] = []
        candidate_seen = False
        for entry in sequence(load(raw)["dependencies"]):
            package = mapping(entry)
            name = canonical(identifier(package["name"]))
            if name == self.name:
                if (
                    candidate_seen
                    or package.get("version") != self.version
                    or "skip_reason" in package
                ):
                    raise EvidenceError("Unexpected candidate audit entry")
                candidate_seen = True
                continue
            if "skip_reason" in package:
                continue
            observed.append(f"{name}=={identifier(package['version'])}")
        if tuple(sorted(observed)) != requirements:
            raise EvidenceError("Audited third-party inventory does not match installed inventory")
