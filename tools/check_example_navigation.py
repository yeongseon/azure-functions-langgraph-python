"""Resolve example-guide repository links without network access."""

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/examples/index.md"
PREFIX = "/yeongseon/azure-functions-langgraph-python/blob/main/"


def main() -> int:
    failures = []
    checked = 0
    examples = set()
    for target in re.findall(r"\]\(([^)]+)\)", GUIDE.read_text(encoding="utf-8")):
        url = urlsplit(target)
        if url.scheme:
            if url.netloc != "github.com" or not url.path.startswith(PREFIX):
                continue
            relative = unquote(url.path[len(PREFIX) :])
            path = ROOT / relative
            if (
                len(Path(relative).parts) == 3
                and relative.startswith("examples/")
                and relative.endswith("/README.md")
            ):
                examples.add(path.parent.name)
        else:
            path = GUIDE.parent / unquote(url.path)
        checked += 1
        if not path.is_file():
            failures.append(f"Missing target: {target}")
    expected = {path.parent.name for path in (ROOT / "examples").glob("*/README.md")}
    failures.extend(f"Unlisted example: {name}" for name in sorted(expected - examples))
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Checked {checked} repository links and {len(examples)} example READMEs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
