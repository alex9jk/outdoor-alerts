"""The object store the pipeline stages hand their results through."""

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from gooddays.objectstore import (
    InMemoryObjectStore,
    LocalDirectoryObjectStore,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
)

KEY = "runs/exec-1/fetch.json"


@pytest.fixture(params=["memory", "local"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ObjectStore:
    if request.param == "memory":
        return InMemoryObjectStore()
    return LocalDirectoryObjectStore(tmp_path / "bucket")


def test_what_is_put_comes_back(store: ObjectStore) -> None:
    data = {"a": [1, 2.5, None, True], "b": {"c": "d"}}

    store.put_json(KEY, data)

    assert store.get_json(KEY) == data


def test_putting_the_same_key_again_replaces_it(store: ObjectStore) -> None:
    store.put_json(KEY, {"v": 1})
    store.put_json(KEY, {"v": 2})

    assert store.get_json(KEY) == {"v": 2}


def test_a_missing_key_raises_an_error_that_names_it(store: ObjectStore) -> None:
    with pytest.raises(ObjectNotFoundError, match="runs/exec-1/nope.json") as e:
        store.get_json("runs/exec-1/nope.json")

    assert e.value.key == "runs/exec-1/nope.json"
    assert isinstance(e.value, ObjectStoreError)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/abs/key",
        "../up.json",
        "a/../b.json",
        "a//b.json",
        "a\\b.json",
        "a/./b",
        "a\x00b",
    ],
)
def test_bad_keys_are_rejected_on_both_put_and_get(
    store: ObjectStore, key: str
) -> None:
    with pytest.raises(ObjectStoreError, match="invalid object key"):
        store.put_json(key, {})
    with pytest.raises(ObjectStoreError, match="invalid object key"):
        store.get_json(key)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), object(), {1, 2}])
def test_data_that_is_not_json_is_refused_and_nothing_is_stored(
    store: ObjectStore, bad: Any
) -> None:
    with pytest.raises(ObjectStoreError, match="not JSON-serializable"):
        store.put_json(KEY, {"x": bad})

    with pytest.raises(ObjectNotFoundError):
        store.get_json(KEY)


def test_the_memory_store_hands_back_copies() -> None:
    store = InMemoryObjectStore()
    data = {"list": [1]}
    store.put_json(KEY, data)
    data["list"].append(2)

    got = store.get_json(KEY)
    got["list"].append(3)

    assert store.get_json(KEY) == {"list": [1]}


def test_the_local_store_writes_under_its_root_and_leaves_no_temp_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bucket"
    store = LocalDirectoryObjectStore(root)

    store.put_json(KEY, {"v": 1})
    store.put_json(KEY, {"v": 2})

    assert [p.name for p in (root / "runs" / "exec-1").iterdir()] == ["fetch.json"]


def test_the_local_store_does_not_follow_a_link_out_of_its_root(tmp_path: Path) -> None:
    root = tmp_path / "bucket"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "runs").mkdir(parents=True)
    (root / "runs" / "link").symlink_to(outside)
    store = LocalDirectoryObjectStore(root)

    with pytest.raises(ObjectStoreError, match="invalid object key"):
        store.put_json("runs/link/x.json", {})

    assert list(outside.iterdir()) == []


def test_corrupt_stored_data_is_an_error_that_names_the_key(tmp_path: Path) -> None:
    root = tmp_path / "bucket"
    (root / "runs" / "exec-1").mkdir(parents=True)
    (root / "runs" / "exec-1" / "fetch.json").write_text("{oops")

    with pytest.raises(ObjectStoreError, match="runs/exec-1/fetch.json"):
        LocalDirectoryObjectStore(root).get_json(KEY)


def test_a_file_that_is_not_utf8_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "bucket"
    (root / "runs" / "exec-1").mkdir(parents=True)
    (root / "runs" / "exec-1" / "fetch.json").write_bytes(b"\xff\xfe\x00")

    with pytest.raises(ObjectStoreError, match="cannot read"):
        LocalDirectoryObjectStore(root).get_json(KEY)


@pytest.mark.parametrize("module", ["gooddays.objectstore", "gooddays.serialize"])
def test_importing_these_modules_does_not_load_boto3(module: str) -> None:
    # A fresh interpreter: the test session itself has already imported boto3.
    code = f"import sys, {module}; assert 'boto3' not in sys.modules"

    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
