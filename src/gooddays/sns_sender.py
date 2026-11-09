"""The AWS SNS sender: one `publish` per message, behind `NotificationSender`.

We publish to an SNS topic; the email address is a subscription on that topic,
created and confirmed outside the code. A subscriber who has not confirmed
their subscription gets nothing, and SNS still accepts the publish.

Credentials come from the standard AWS chain (environment variables, a profile,
or an IAM role); the region is taken from the topic ARN. This is the only module
that imports boto3.
"""

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    ReadTimeoutError,
    TokenRetrievalError,
)

from gooddays.notify import NotificationSender, SendError, topic_region

TIMEOUT_SECONDS = 10
# botocore retries by default, including after read timeouts, and a publish has no
# idempotency key, so a hidden retry could notify twice. One try; notify.py retries
# only the cases we have decided are safe.
_CONFIG = Config(
    connect_timeout=TIMEOUT_SECONDS,
    read_timeout=TIMEOUT_SECONDS,
    retries={"total_max_attempts": 1},
)

_AUTH_CODES = frozenset(
    {
        "AuthorizationError",
        "AccessDenied",
        "AccessDeniedException",
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "ExpiredTokenException",
    }
)
_NOT_FOUND_CODES = frozenset({"NotFound", "NotFoundException"})
_THROTTLE_CODES = frozenset(
    {"Throttled", "ThrottlingException", "Throttling", "RequestLimitExceeded"}
)


def _new_client(region: str) -> Any:
    """The one place a real AWS client is made (tests replace it)."""
    return boto3.client("sns", region_name=region, config=_CONFIG)


def sns_email_sender() -> NotificationSender:
    """A `NotificationSender` that publishes to an SNS topic (clients built on use)."""
    clients: dict[str, Any] = {}

    def send(topic_arn: str, subject: str, body: str) -> str:
        region = topic_region(topic_arn)
        if region not in clients:
            clients[region] = _new_client(region)
        try:
            response = clients[region].publish(
                TopicArn=topic_arn, Subject=subject, Message=body
            )
        except (
            NoCredentialsError,
            PartialCredentialsError,
            CredentialRetrievalError,
            ProfileNotFound,
            TokenRetrievalError,
        ):
            raise SendError("no_credentials") from None
        except ConnectTimeoutError:
            raise SendError("connect_timeout", retryable=True) from None
        except EndpointConnectionError:
            raise SendError("connection_error", retryable=True) from None
        except ConnectionClosedError:
            # The request may have been sent before the connection closed.
            raise SendError("connection_error") from None
        except ReadTimeoutError:
            raise SendError("read_timeout") from None
        except ClientError as error:
            raise _client_error(error) from None
        except BotoCoreError:
            raise SendError("request_failed") from None
        message_id = response.get("MessageId") if isinstance(response, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise SendError("bad_response")
        return message_id

    return send


def _client_error(error: ClientError) -> SendError:
    """Map an AWS error to a reason key. Only the code and status are used: the
    message text is never kept, since AWS error messages can echo identifiers."""
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    if code in _AUTH_CODES or status in (401, 403):
        return SendError("auth_rejected")
    if code in _NOT_FOUND_CODES or status == 404:
        return SendError("topic_not_found")
    if code in _THROTTLE_CODES or status == 429:
        return SendError("throttled")
    if status >= 500:
        return SendError("unavailable", retryable=True)
    return SendError("rejected")
