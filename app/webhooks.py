# printpapi — self-hosted PrintNode alternative. Elastic License 2.0 (see LICENSE).
"""Account webhooks: what a valid one looks like. Pure — the store keeps them, the server's
dispatcher delivers them, app/printapi.py dresses them in the compatible shapes.

A webhook is a target URL, a secret sent along with every request (`X-Webhook-Secret`) so the
target can tell the message is ours, and the message types it wants. Modelled on the compatible
API's account webhooks so an integration written for it needs only a new base URL."""
from urllib.parse import urlparse

COMPUTER_STATE = "computer state"
PRINT_JOB_STATE = "print job state"
MESSAGE_TYPES = (COMPUTER_STATE, PRINT_JOB_STATE)
MAX_PER_ORG = 5          # the compatible API's limit; it also bounds the fan-out of every job event
SECRET_HEADER = "X-Webhook-Secret"


def wants(messages, type_):
    return "*" in messages or type_ in messages


def validate(body, partial=False):
    """A create (all of url, secret, messages) or, with `partial`, an update (any of them) ->
    the cleaned fields. Raises ValueError naming the bad field."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    out = {}
    if "url" in body or not partial:
        url = body.get("url")
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") \
                or not urlparse(url).netloc:
            raise ValueError(f"url must be an http(s) URL: {url!r}")
        out["url"] = url
    if "secret" in body or not partial:
        secret = body.get("secret")
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("secret must be a non-empty string")
        out["secret"] = secret
    if "messages" in body or not partial:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages \
                or not all(isinstance(m, str) for m in messages):
            raise ValueError('messages must be a non-empty list, e.g. ["*"]')
        if "*" in messages and len(messages) > 1:
            raise ValueError('messages: "*" means every type and must stand alone')
        unknown = [m for m in messages if m != "*" and m not in MESSAGE_TYPES]
        if unknown:
            raise ValueError(f"messages: unknown type {unknown[0]!r} "
                             f"(supported: *, {', '.join(MESSAGE_TYPES)})")
        out["messages"] = list(dict.fromkeys(messages))
    if not out:
        raise ValueError("nothing to update: give url, secret or messages")
    return out
