"""The S3 object store, with a stubbed client: no request ever leaves the process."""

import io
import json
from typing import Any

import boto3
import pytest
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError
from botocore.stub import Stubber

from gooddays import s3_store
from gooddays.objectstore import ObjectNotFoundError, ObjectStoreError
from gooddays.s3_store import S3ObjectStore, map_error

KEY = "runs/exec-1/fetch.json"
BUCKET = "gooddays-runs-test"


@pytest.fixture
def client() -> Any:
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
        aws_secret_access_key="fake-secret-not-real",
    )


def test_put_sends_the_json_to_the_bucket(client: Any) -> None:
    store = S3ObjectStore(BUCKET, client)
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": KEY,
                "Body": stub_any_body(),
                "ContentType": "application/json",
            },
        )
        store.put_json(KEY, {"v": 1})
        stub.assert_no_pending_responses()


def stub_any_body() -> Any:
    from botocore.stub import ANY

    return ANY


def test_get_returns_the_stored_json(client: Any) -> None:
    store = S3ObjectStore(BUCKET, client)
    body = io.BytesIO(json.dumps({"v": 1}).encode())
    with Stubber(client) as stub:
        stub.add_response(
            "get_object",
            {"Body": _streaming(body)},
            {"Bucket": BUCKET, "Key": KEY},
        )
        assert store.get_json(KEY) == {"v": 1}


def _streaming(body: io.BytesIO) -> Any:
    from botocore.response import StreamingBody

    data = body.getvalue()
    return StreamingBody(io.BytesIO(data), len(data))


def test_a_missing_object_is_object_not_found_naming_the_key(client: Any) -> None:
    store = S3ObjectStore(BUCKET, client)
    with Stubber(client) as stub:
        stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
        with pytest.raises(ObjectNotFoundError, match=KEY):
            store.get_json(KEY)


def test_other_s3_errors_keep_only_the_code_and_status(client: Any) -> None:
    store = S3ObjectStore(BUCKET, client)
    with Stubber(client) as stub:
        stub.add_client_error(
            "get_object",
            "AccessDenied",
            service_message="secret detail from AWS",
            http_status_code=403,
        )
        with pytest.raises(ObjectStoreError) as e:
            store.get_json(KEY)

    assert "AccessDenied" in str(e.value) and "403" in str(e.value)
    assert "secret detail" not in str(e.value)
    assert not isinstance(e.value, ObjectNotFoundError)


def test_a_network_failure_is_an_object_store_error() -> None:
    error = map_error(KEY, ConnectTimeoutError(endpoint_url="https://example.invalid"))

    assert isinstance(error, ObjectStoreError) and KEY in str(error)
    assert "example.invalid" not in str(error)


def test_bad_keys_and_an_empty_bucket_are_refused_before_any_request() -> None:
    with pytest.raises(ObjectStoreError, match="no S3 bucket"):
        S3ObjectStore("")
    with pytest.raises(ObjectStoreError, match="invalid object key"):
        S3ObjectStore(BUCKET, object()).put_json("../x", {})


def test_no_client_is_made_until_the_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[str] = []
    monkeypatch.setattr(s3_store, "_new_client", lambda: made.append("client"))

    S3ObjectStore(BUCKET)

    assert made == []


def _get(client: Any, data: bytes) -> Any:
    with Stubber(client) as stub:
        stub.add_response(
            "get_object",
            {"Body": _streaming(io.BytesIO(data))},
            {"Bucket": BUCKET, "Key": KEY},
        )
        return S3ObjectStore(BUCKET, client).get_json(KEY)


def test_a_body_that_is_not_json_names_the_key_and_hides_the_content(
    client: Any,
) -> None:
    with pytest.raises(ObjectStoreError, match=KEY) as e:
        _get(client, b"secret-token {not json")

    assert "secret-token" not in str(e.value)
    assert not isinstance(e.value, ObjectNotFoundError)


def test_a_body_that_is_not_utf8_names_the_key_and_hides_the_content(
    client: Any,
) -> None:
    with pytest.raises(ObjectStoreError, match=KEY) as e:
        _get(client, b'{"v": "\xff\xfe secret-token"}')

    assert "secret-token" not in str(e.value)
    assert "xff" not in str(e.value)


def test_an_empty_body_is_an_object_store_error(client: Any) -> None:
    with pytest.raises(ObjectStoreError, match=KEY):
        _get(client, b"")


def test_a_failure_while_reading_the_body_is_an_object_store_error(
    client: Any,
) -> None:
    class BrokenBody:
        def read(self) -> bytes:
            raise ReadTimeoutError(endpoint_url="https://secret.example/")

    with Stubber(client) as stub:
        stub.add_response("get_object", {"Body": BrokenBody()})
        with pytest.raises(ObjectStoreError, match=KEY) as e:
            S3ObjectStore(BUCKET, client).get_json(KEY)

    assert "secret.example" not in str(e.value)
