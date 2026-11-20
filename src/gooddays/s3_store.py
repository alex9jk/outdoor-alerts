"""The S3 version of the object store: one JSON object per key in one bucket.

This module imports boto3 at the top, so import it only where it is used (the
handlers do it lazily). Credentials come from the usual AWS chain (the Lambda's role).
"""

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from gooddays.objectstore import (
    ObjectNotFoundError,
    ObjectStoreError,
    decode_json,
    encode_json,
    validate_key,
)

_MISSING_CODES = {"NoSuchKey", "404", "NotFound"}


def _new_client() -> Any:
    """The single place a real S3 client is built (tests replace it)."""
    return boto3.client("s3", config=Config(connect_timeout=10, read_timeout=10))


def map_error(key: str, error: Exception) -> ObjectStoreError:
    """Our error for an S3 failure. Only the error code and status are kept, never
    AWS's message text."""
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in _MISSING_CODES:
            return ObjectNotFoundError(key)
        return ObjectStoreError(f"S3 failed for {key} (code={code}, status={status})")
    return ObjectStoreError(f"cannot reach S3 for {key} ({type(error).__name__})")


class S3ObjectStore:
    def __init__(self, bucket: str, client: Any | None = None) -> None:
        if not bucket:
            raise ObjectStoreError("no S3 bucket name given")
        self._bucket = bucket
        self._client = client

    def _s3(self) -> Any:
        if self._client is None:
            self._client = _new_client()
        return self._client

    def put_json(self, key: str, data: Any) -> None:
        validate_key(key)
        body = encode_json(key, data).encode("utf-8")
        try:
            # Putting the same key again just replaces it, so a retry is safe.
            self._s3().put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
        except (ClientError, BotoCoreError) as e:
            raise map_error(key, e) from None

    def get_json(self, key: str) -> Any:
        validate_key(key)
        try:
            response = self._s3().get_object(Bucket=self._bucket, Key=key)
            body = response["Body"].read()
        except (ClientError, BotoCoreError) as e:
            raise map_error(key, e) from None
        try:
            return decode_json(key, body.decode("utf-8"))
        except UnicodeDecodeError:
            raise ObjectStoreError(f"cannot read {key}") from None
