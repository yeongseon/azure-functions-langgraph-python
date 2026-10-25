"""Versioned Table JSON properties and conservative pre-write capacity checks."""

from collections.abc import Mapping
from datetime import datetime
import json
from typing import Any, Final

from .._json import dumps

PROPERTY_BYTES: Final = 64 * 1024
ENTITY_BYTES: Final = 1024 * 1024
_CHUNK_CHARACTERS: Final = PROPERTY_BYTES // 2


class TableStorageLimitError(ValueError):
    """Content cannot be represented safely in a single Azure Table entity."""

    def __init__(self, dimension: str, size: int, limit: int) -> None:
        self.dimension = dimension
        self.size = size
        self.limit = limit
        super().__init__(
            f"Azure Table {dimension} size {size} exceeds limit {limit}; "
            "reduce thread data or use a custom ThreadStore backed by Blob storage."
        )


def write_json(entity: dict[str, Any], field: str, value: Any) -> None:
    encoded = dumps(value, allow_nan=False)
    for key in list(entity):
        if key == field or key.startswith(f"{field}_"):
            del entity[key]
    if len(encoded.encode("utf-16-le")) <= PROPERTY_BYTES:
        entity[field] = encoded
        return
    entity[f"{field}_format"] = "utf16-v1"
    chunks = [encoded[i : i + _CHUNK_CHARACTERS] for i in range(0, len(encoded), _CHUNK_CHARACTERS)]
    entity[f"{field}_count"] = len(chunks)
    for index, chunk in enumerate(chunks):
        entity[f"{field}_{index}"] = chunk


def read_json(entity: Mapping[str, Any], field: str, default: Any = None) -> Any:
    if f"{field}_format" in entity:
        if entity[f"{field}_format"] != "utf16-v1":
            raise ValueError(f"Unsupported Azure Table JSON format for {field}")
        count = entity[f"{field}_count"]
        if not isinstance(count, int) or not 1 <= count <= 252:
            raise ValueError(f"Invalid Azure Table JSON chunk count for {field}")
        encoded = "".join(entity[f"{field}_{index}"] for index in range(count))
        return json.loads(encoded)
    legacy = entity.get(field)
    return default if legacy is None else json.loads(legacy)


def validate_entity(entity: Mapping[str, Any]) -> None:
    # Reserve space for Timestamp, service overhead and future run-lock properties.
    size = 4096
    properties = 1
    for name, value in entity.items():
        if name in {"etag", "Timestamp"} or value is None:
            continue
        properties += 1
        size += len(name.encode("utf-16-le")) + 32
        match value:
            case str():
                value_size = len(value.encode("utf-16-le"))
                if value_size > PROPERTY_BYTES:
                    raise TableStorageLimitError("string property", value_size, PROPERTY_BYTES)
                size += value_size
            case bytes():
                if len(value) > PROPERTY_BYTES:
                    raise TableStorageLimitError("binary property", len(value), PROPERTY_BYTES)
                size += len(value)
            case bool() | int() | float() | datetime():
                size += 16
            case _:
                raise TypeError(f"Unsupported Azure Table property type: {type(value).__name__}")
    if properties > 255:
        raise TableStorageLimitError("property count", properties, 255)
    if size > ENTITY_BYTES:
        raise TableStorageLimitError("entity", size, ENTITY_BYTES)
