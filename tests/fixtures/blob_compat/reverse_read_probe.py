"""Manual downgrade probe: can an OLDER saver read a committed fixture set?

Not run by the test suite (it needs a venv with a published wheel installed).
It is how the downgrade statements in docs/blob-checkpoint-format.md were
produced. Usage, from a throwaway venv that has the old wheel installed:

    python reverse_read_probe.py sets/current [--simulate-crash-after-dirty-marker]

It reads every thread/namespace in ``expected.json`` and prints, per thread,
whether the newest checkpoint was found, whether it matched the expectation,
and any exception. It never writes.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import ContainerClient
from azure.storage.blob.aio import ContainerClient as AioContainerClient
from langchain_core.runnables import RunnableConfig

sys.path.insert(0, str(Path(__file__).parent))
from generate_fixtures import make_containers  # noqa: E402

from azure_functions_langgraph.checkpointers.azure_blob import (
    AzureBlobCheckpointSaver,  # noqa: E402
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("set_dir", type=Path)
    parser.add_argument("--simulate-crash-after-dirty-marker", action="store_true")
    args = parser.parse_args()
    raw = json.loads((args.set_dir / "blobs.json").read_text())
    expected = json.loads((args.set_dir / "expected.json").read_text())
    blobs = {b["name"]: (base64.b64decode(b["data_b64"]), b["metadata"]) for b in raw["blobs"]}
    if args.simulate_crash_after_dirty_marker:
        for name in list(blobs):
            if name.endswith("/latest.json"):
                blobs[name] = (b'{"version":1,"dirty":true}', {})
    sync_cls, _ = make_containers(ContainerClient, AioContainerClient, ResourceNotFoundError)
    saver = AzureBlobCheckpointSaver(container_client=sync_cls(blobs))
    for entry in expected["writes"]:
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": entry["thread_id"],
                "checkpoint_ns": entry["checkpoint_ns"],
            }
        }
        newest = entry["checkpoints"][-1]
        try:
            tup = saver.get_tuple(cfg)
        except Exception as exc:  # noqa: BLE001 - probe reports every outcome
            label = f"{entry['thread_id']!r} ns={entry['checkpoint_ns']!r}"
            print(f"{label}: ERROR {type(exc).__name__}: {exc}")
            continue
        if tup is None:
            print(f"{entry['thread_id']!r} ns={entry['checkpoint_ns']!r}: NONE")
            continue
        id_ok = tup.checkpoint["id"] == newest["id"]
        values_ok = tup.checkpoint["channel_values"] == newest["channel_values"]
        got_writes = [list(w) for w in tup.pending_writes or []]
        writes_ok = got_writes == newest["pending_writes"]
        same_set = sorted(map(repr, got_writes)) == sorted(map(repr, newest["pending_writes"]))
        writes = "ok" if writes_ok else ("same-set-different-order" if same_set else "WRONG")
        print(
            f"{entry['thread_id']!r} ns={entry['checkpoint_ns']!r}: "
            f"newest_id={'ok' if id_ok else 'WRONG'} values={'ok' if values_ok else 'WRONG'} "
            f"pending_writes={writes}"
        )


if __name__ == "__main__":
    main()
