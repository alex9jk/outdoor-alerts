"""Where the pipeline stages leave their results for the next stage (JSON by key).

The stages run as separate Lambdas, so each hands the next one a key, not the data
(Step Functions caps a payload at 256 KB). This module never imports boto3: the S3
version lives in `s3_store.py` and is imported only where it is used.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol


class ObjectStoreError(Exception):
    """The store could not read or write an object."""


class ObjectNotFoundError(ObjectStoreError):
    def __init__(self, key: str) -> None:
        super().__init__(f"object not found: {key}")
        self.key = key


class ObjectStore(Protocol):
    def put_json(self, key: str, data: Any) -> None:
        """Store `data` under `key`, replacing anything there."""
        ...

    def get_json(self, key: str) -> Any:
        """The data under `key`. Raises ObjectNotFoundError when there is none."""
        ...


def validate_key(key: str) -> None:
    """Keys look like `runs/<id>/fetch.json`: relative, with no way out of the root."""
    if (
        not key
        or key.startswith("/")
        or "\\" in key
        or any(c < " " for c in key)
        or any(part in ("", ".", "..") for part in key.split("/"))
    ):
        raise ObjectStoreError(f"invalid object key: {key!r}")


def _dumps(key: str, data: Any) -> str:
    try:
        return json.dumps(data, allow_nan=False, indent=2) + "\n"
    except (TypeError, ValueError) as e:
        raise ObjectStoreError(f"{key} is not JSON-serializable: {e}") from e


def _loads(key: str, text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as e:
        raise ObjectStoreError(f"{key} does not contain valid JSON") from e


class InMemoryObjectStore:
    """For tests. Keeps the JSON text, so reads return a copy, never the original."""

    def __init__(self) -> None:
        self._objects: dict[str, str] = {}

    def put_json(self, key: str, data: Any) -> None:
        validate_key(key)
        self._objects[key] = _dumps(key, data)

    def get_json(self, key: str) -> Any:
        validate_key(key)
        try:
            text = self._objects[key]
        except KeyError:
            raise ObjectNotFoundError(key) from None
        return _loads(key, text)

    def keys(self) -> list[str]:
        return sorted(self._objects)


class LocalDirectoryObjectStore:
    """Keys are paths under `root`. Writes are all-or-nothing (temp file, rename)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ObjectStoreError(f"invalid object key: {key!r}")
        return path

    def put_json(self, key: str, data: Any) -> None:
        path = self._path(key)
        text = _dumps(key, data)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                dir=path.parent, prefix=".obj-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_name, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)
                raise
        except OSError as e:
            raise ObjectStoreError(f"cannot write {key}: {e.strerror or e}") from e

    def get_json(self, key: str) -> Any:
        path = self._path(key)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ObjectNotFoundError(key) from None
        except (OSError, UnicodeDecodeError) as e:
            raise ObjectStoreError(f"cannot read {key}") from e
        return _loads(key, text)
