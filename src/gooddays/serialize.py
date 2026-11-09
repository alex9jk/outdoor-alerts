"""JSON for what one pipeline stage hands to the next, and back, strictly.

Each stored file is `{"schema": <name>, "version": 1, "data": {...}}`. Reading never
guesses: a missing or unknown key, a wrong type, a value outside a Literal, a naive
datetime, NaN or an unknown version raises SerializationError. Errors name the path
and the expected type, never the offending value (it may be sensitive).

The encoder and decoder walk the dataclasses' type hints, so a new model field is
picked up (and checked) without touching this file.

Datetimes are written with their numeric UTC offset and come back with that fixed
offset, not the IANA zone. Compare them through UTC (the scorer and the message code
already do): two datetimes in a repeated DST hour compare unequal across zones.
"""

import dataclasses
import math
import types
import typing
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Union, cast

from gooddays.models import FetchResult, Message, MessageFacts, ScoreResult

SCHEMA_VERSION = 1


class SerializationError(ValueError):
    """The data is malformed, incomplete, of the wrong type or the wrong schema."""


class UnsupportedSchemaVersionError(SerializationError):
    """The file was written by a different version of the schema."""


@dataclass(frozen=True, slots=True)
class MessageStage:
    """What Message hands to Send: the new facts and the text (None = nothing)."""

    facts: MessageFacts
    message: Message | None


SCHEMAS: dict[str, type] = {
    "fetch": FetchResult,
    "score": ScoreResult,
    "message": MessageStage,
}


def serialize(schema: str, value: Any) -> dict[str, Any]:
    """The envelope for `value`, ready for `ObjectStore.put_json`."""
    cls = _schema_class(schema)
    if not isinstance(value, cls):
        raise SerializationError(f"{schema}: expected {cls.__name__}")
    return {
        "schema": schema,
        "version": SCHEMA_VERSION,
        "data": _encode(value, cls, schema),
    }


def deserialize(schema: str, document: Any) -> Any:
    """The value in an envelope read from the store."""
    cls = _schema_class(schema)
    if not isinstance(document, dict) or set(document) != {"schema", "version", "data"}:
        raise SerializationError(f"{schema}: not a {schema} document")
    if document["schema"] != schema:
        raise SerializationError(f"{schema}: wrong schema (expected {schema!r})")
    version = document["version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"{schema}: unsupported version (expected {SCHEMA_VERSION})"
        )
    return _decode(document["data"], cls, schema)


def _schema_class(schema: str) -> type:
    try:
        return SCHEMAS[schema]
    except KeyError:
        raise SerializationError(f"unknown schema {schema!r}") from None


def _fail(path: str, expected: str) -> SerializationError:
    return SerializationError(f"{path}: expected {expected}")


def _hint_name(hint: Any) -> str:
    return getattr(hint, "__name__", None) or str(hint)


def _encode(value: Any, hint: Any, path: str) -> Any:
    return _walk(value, hint, path, encode=True)


def _decode(data: Any, hint: Any, path: str) -> Any:
    return _walk(data, hint, path, encode=False)


def _walk(value: Any, hint: Any, path: str, *, encode: bool) -> Any:
    """Check `value` against `hint` and convert it (to JSON data, or back from it)."""
    origin = typing.get_origin(hint)

    if origin is Union or origin is types.UnionType:
        args = typing.get_args(hint)
        if len(args) != 2 or type(None) not in args:
            raise TypeError(f"{path}: unsupported union {hint}")  # a bug, not bad data
        if value is None:
            return None
        inner = next(a for a in args if a is not type(None))
        return _walk(value, inner, path, encode=encode)

    if origin is Literal:
        allowed = typing.get_args(hint)
        if not any(type(value) is type(a) and value == a for a in allowed):
            raise _fail(path, "one of " + ", ".join(repr(a) for a in allowed))
        return value

    if origin in (tuple, list):
        args = typing.get_args(hint)
        if len(args) != 2 or args[1] is not Ellipsis:
            if origin is list and len(args) == 1:
                args = (args[0], Ellipsis)
            else:
                raise TypeError(f"{path}: unsupported sequence {hint}")
        want = (tuple, list) if encode else (list,)
        if not isinstance(value, want):
            raise _fail(path, "a list" if not encode else "a sequence")
        items = [
            _walk(item, args[0], f"{path}[{i}]", encode=encode)
            for i, item in enumerate(value)
        ]
        if encode:
            return items
        return tuple(items) if origin is tuple else items

    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        return _walk_dataclass(value, hint, path, encode=encode)

    if hint is bool:
        if not isinstance(value, bool):
            raise _fail(path, "a boolean")
        return value
    if hint is str:
        if not isinstance(value, str):
            raise _fail(path, "a string")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(path, "an integer")
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _fail(path, "a number")
        if not math.isfinite(value):
            raise _fail(path, "a finite number")
        return float(value)
    if hint is datetime:
        return _walk_datetime(value, path, encode=encode)
    if hint is date:
        return _walk_date(value, path, encode=encode)
    raise TypeError(f"{path}: unsupported type {_hint_name(hint)}")  # a bug


def _walk_dataclass(value: Any, cls: type, path: str, *, encode: bool) -> Any:
    hints = typing.get_type_hints(cls)
    names = [f.name for f in dataclasses.fields(cls)]
    if encode:
        if not isinstance(value, cls):
            raise _fail(path, cls.__name__)
        return {
            name: _walk(
                getattr(value, name), hints[name], f"{path}.{name}", encode=True
            )
            for name in names
        }
    if not isinstance(value, dict):
        raise _fail(path, f"an object ({cls.__name__})")
    missing = [n for n in names if n not in value]
    unknown = sorted(str(k) for k in value if k not in names)
    if missing or unknown:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if unknown:
            parts.append("unknown " + ", ".join(unknown))
        raise SerializationError(f"{path}: {'; '.join(parts)}")
    return cls(
        **{
            name: _walk(value[name], hints[name], f"{path}.{name}", encode=False)
            for name in names
        }
    )


def _walk_datetime(value: Any, path: str, *, encode: bool) -> Any:
    if encode:
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise _fail(path, "a timezone-aware datetime")
        return value.isoformat()
    if not isinstance(value, str):
        raise _fail(path, "a datetime string with a UTC offset")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _fail(path, "an ISO 8601 datetime") from None
    if parsed.utcoffset() is None:
        raise _fail(path, "a datetime with a UTC offset (no naive times)")
    return parsed


def _walk_date(value: Any, path: str, *, encode: bool) -> Any:
    if encode:
        if not isinstance(value, date) or isinstance(value, datetime):
            raise _fail(path, "a date")
        return value.isoformat()
    if not isinstance(value, str) or len(value) != 10:
        raise _fail(path, "a date as YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _fail(path, "a date as YYYY-MM-DD") from None


def serialize_fetch(result: FetchResult) -> dict[str, Any]:
    return serialize("fetch", result)


def deserialize_fetch(document: Any) -> FetchResult:
    return cast(FetchResult, deserialize("fetch", document))


def serialize_score(result: ScoreResult) -> dict[str, Any]:
    return serialize("score", result)


def deserialize_score(document: Any) -> ScoreResult:
    return cast(ScoreResult, deserialize("score", document))


def serialize_message_stage(stage: MessageStage) -> dict[str, Any]:
    return serialize("message", stage)


def deserialize_message_stage(document: Any) -> MessageStage:
    return cast(MessageStage, deserialize("message", document))
