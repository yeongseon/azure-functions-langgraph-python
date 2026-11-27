"""Checkpoint key layout and RunnableConfig conversion, without storage I/O."""

from __future__ import annotations

from urllib.parse import quote, unquote

from langchain_core.runnables import RunnableConfig


class _BlobLayout:
    def _parse_value_blob_path(
        self, thread_id: str, checkpoint_ns: str, blob_path: str
    ) -> tuple[str, str] | None:
        values_prefix = f"{self._namespace_prefix(thread_id, checkpoint_ns)}values/"
        if not blob_path.startswith(values_prefix):
            return None
        relative = blob_path[len(values_prefix) :]
        parts = relative.split("/")
        if len(parts) != 2 or not parts[1].endswith(".bin"):
            return None
        channel = unquote(parts[0])
        version = unquote(parts[1].removesuffix(".bin"))
        return (channel, version)

    def _checkpoint_config(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def _thread_prefix(self, thread_id: str) -> str:
        return f"threads/{self._escape(thread_id)}/"

    def _namespace_prefix(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._thread_prefix(thread_id)}ns/{self._escape(checkpoint_ns)}/"

    def _checkpoints_prefix(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._namespace_prefix(thread_id, checkpoint_ns)}checkpoints/"

    def _checkpoint_base_prefix(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> str:
        return f"{self._checkpoints_prefix(thread_id, checkpoint_ns)}{self._escape(checkpoint_id)}/"

    def _checkpoint_blob_path(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return (
            f"{self._checkpoint_base_prefix(thread_id, checkpoint_ns, checkpoint_id)}checkpoint.bin"
        )

    def _metadata_blob_path(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return (
            f"{self._checkpoint_base_prefix(thread_id, checkpoint_ns, checkpoint_id)}metadata.bin"
        )

    def _writes_prefix(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"{self._checkpoint_base_prefix(thread_id, checkpoint_ns, checkpoint_id)}writes/"

    def _write_blob_path(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        task_id: str,
        write_index: int,
    ) -> str:
        return (
            f"{self._writes_prefix(thread_id, checkpoint_ns, checkpoint_id)}"
            f"{self._escape(task_id)}/{self._escape(str(write_index))}.bin"
        )

    def _value_blob_path(
        self,
        thread_id: str,
        checkpoint_ns: str,
        channel: str,
        version: str | int | float,
    ) -> str:
        return (
            f"{self._namespace_prefix(thread_id, checkpoint_ns)}values/"
            f"{self._escape(channel)}/{self._escape(str(version))}.bin"
        )

    def _latest_blob_path(self, thread_id: str, checkpoint_ns: str) -> str:
        return f"{self._namespace_prefix(thread_id, checkpoint_ns)}latest.json"

    def _config_thread_id(self, config: RunnableConfig) -> str:
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise ValueError("configurable must be provided in RunnableConfig")

        thread_id = configurable.get("thread_id")
        if thread_id is None:
            raise ValueError("thread_id is required in config.configurable")
        return str(thread_id)

    def _config_checkpoint_ns(self, config: RunnableConfig) -> str:
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise ValueError("configurable must be provided in RunnableConfig")
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        return str(checkpoint_ns)

    def _escape(self, segment: str) -> str:
        return quote(segment, safe="")
