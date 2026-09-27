"""Signed webhooks with a transactional outbox and retries.

Receivers verify a delivery by recomputing

    HMAC-SHA256(secret, f"{timestamp}.{raw_body}")

and comparing it with the v1 value in the `X-BananaHat-Signature` header
(`t=<timestamp>,v1=<hex>`). Rejecting timestamps older than five minutes stops replays.
README.md has a ten-line receiver.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.request
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.db import connection, transaction

from core import clock
from integrations.models import WebhookDelivery, WebhookEndpoint

log = logging.getLogger(__name__)

USER_AGENT = "BananaHat-Webhooks/1"
MAX_BACKOFF = timedelta(hours=6)


def emit(event, event_type, data):
    """Queue `event_type` for every endpoint of `event` that wants it. Call inside the
    transaction that makes the change; nothing is sent until it commits."""
    endpoints = [e for e in WebhookEndpoint.objects.filter(event=event, is_active=True) if e.wants(event_type)]
    now = clock.now()
    deliveries = []
    for endpoint in endpoints:
        delivery_id = "dlv_" + uuid.uuid4().hex
        deliveries.append(WebhookDelivery(
            endpoint=endpoint, delivery_id=delivery_id, event_type=event_type,
            payload={"id": delivery_id, "type": event_type, "created_at": now.isoformat(),
                     "event": event.public_id, "data": data},
        ))
    WebhookDelivery.objects.bulk_create(deliveries)
    return deliveries


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify_signature(secret: str, header: str, body: bytes, *, tolerance=300, now=None) -> bool:
    """What a receiver does. Used by our tests and shown in the README."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
    except (ValueError, KeyError):
        return False
    now = int(now if now is not None else clock.now().timestamp())
    if abs(now - timestamp) > tolerance:
        return False
    expected = sign(secret, timestamp, body).split("v1=")[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


class UnsafeTarget(Exception):
    pass


def check_target(url: str):
    """Refuse receivers on loopback, private or link-local addresses (SSRF).

    An organizer should not be able to make the portal POST into its own database
    container or a cloud metadata service. WEBHOOK_ALLOW_PRIVATE_TARGETS turns this
    off for local testing.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeTarget("only http and https URLs are allowed")
    if settings.WEBHOOK_ALLOW_PRIVATE_TARGETS:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeTarget(f"cannot resolve {parsed.hostname}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved \
                or address.is_multicast or address.is_unspecified:
            raise UnsafeTarget(f"{parsed.hostname} resolves to a private address ({address})")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could point at a private address after the check; do not follow."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def deliver(delivery: WebhookDelivery) -> bool:
    endpoint = delivery.endpoint
    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    timestamp = int(clock.now().timestamp())
    delivery.attempts += 1
    try:
        check_target(endpoint.url)
        # check_target above has already refused anything but http(s) to a public address.
        request = urllib.request.Request(endpoint.url, data=body, method="POST", headers={  # noqa: S310
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-BananaHat-Event": delivery.event_type,
            "X-BananaHat-Delivery": delivery.delivery_id,
            "X-BananaHat-Signature": sign(endpoint.secret, timestamp, body),
        })
        with _opener.open(request, timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as response:
            delivery.last_status_code = response.status
        ok = 200 <= delivery.last_status_code < 300
        delivery.last_error = "" if ok else f"HTTP {delivery.last_status_code}"
    except urllib.error.HTTPError as exc:
        ok, delivery.last_status_code, delivery.last_error = False, exc.code, f"HTTP {exc.code}"
    except (UnsafeTarget, urllib.error.URLError, OSError, ValueError) as exc:
        ok, delivery.last_status_code, delivery.last_error = False, None, str(exc)[:300]

    now = clock.now()
    if ok:
        delivery.status = WebhookDelivery.Status.SUCCEEDED
        delivery.delivered_at = now
    elif delivery.attempts >= settings.WEBHOOK_MAX_ATTEMPTS:
        delivery.status = WebhookDelivery.Status.DEAD
    else:
        delivery.status = WebhookDelivery.Status.FAILED
        backoff = min(timedelta(seconds=30 * 2 ** (delivery.attempts - 1)), MAX_BACKOFF)
        delivery.next_attempt_at = now + backoff
    delivery.save()
    return ok


LEASE = timedelta(minutes=2)


def deliver_due(limit=50) -> int:
    """Deliver what is due.

    Rows are claimed in a short transaction (SKIP LOCKED where the database supports
    it) by pushing their next attempt one lease into the future; the HTTP requests
    happen after that transaction commits, so no lock is held while a slow receiver
    thinks. A worker that dies mid-batch just lets its lease expire.
    """
    with transaction.atomic():
        due = (
            WebhookDelivery.objects
            .filter(status__in=[WebhookDelivery.Status.PENDING, WebhookDelivery.Status.FAILED],
                    next_attempt_at__lte=clock.now())
            .order_by("next_attempt_at")
        )
        if connection.features.has_select_for_update_skip_locked:
            due = due.select_for_update(skip_locked=True)
        claimed = list(due.values_list("pk", flat=True)[:limit])
        WebhookDelivery.objects.filter(pk__in=claimed).update(next_attempt_at=clock.now() + LEASE)
    for delivery in WebhookDelivery.objects.select_related("endpoint").filter(pk__in=claimed):
        deliver(delivery)
    return len(claimed)


def ping(endpoint: WebhookEndpoint):
    delivery_id = "dlv_" + uuid.uuid4().hex
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint, delivery_id=delivery_id, event_type="ping",
        payload={"id": delivery_id, "type": "ping", "created_at": clock.now().isoformat(),
                 "event": endpoint.event.public_id, "data": {"message": "Hello from Banana Hat"}},
    )
    deliver(delivery)
    return delivery
