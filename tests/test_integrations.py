"""Webhooks, signed records and the embeddable gallery."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from django.db import transaction

from integrations import webhooks
from integrations.console import create_webhook
from integrations.models import WebhookDelivery
from records import services as records
from tests.conftest import REPO


class _Receiver(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Receiver.received.append((self.headers, body))
        self.send_response(_Receiver.status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def receiver(settings):
    settings.WEBHOOK_ALLOW_PRIVATE_TARGETS = True
    _Receiver.received, _Receiver.status = [], 200
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook", _Receiver
    server.shutdown()


def test_signed_delivery_reaches_receiver(open_event, organizer, participant, receiver):
    url, handler = receiver
    endpoint = create_webhook(open_event, organizer, url=url, event_types=["project.submitted"])
    from events import services

    project = open_event.projects.get(team__memberships__user=participant)
    services.submit_project(project, participant)
    assert webhooks.deliver_due() == 1
    headers, body = handler.received[0]
    assert headers["X-BananaHat-Event"] == "project.submitted"
    assert webhooks.verify_signature(endpoint.secret, headers["X-BananaHat-Signature"], body)
    assert not webhooks.verify_signature("whsec_wrong", headers["X-BananaHat-Signature"], body)
    assert json.loads(body)["data"]["project"] == project.public_id
    assert WebhookDelivery.objects.get().status == WebhookDelivery.Status.SUCCEEDED


def test_rolled_back_change_sends_nothing(open_event, organizer, receiver):
    create_webhook(open_event, organizer, url=receiver[0])
    with pytest.raises(RuntimeError), transaction.atomic():
        webhooks.emit(open_event, "project.submitted", {"project": "x"})
        raise RuntimeError("the change failed")
    assert WebhookDelivery.objects.count() == 0


def test_failed_delivery_backs_off_then_gives_up(open_event, organizer, receiver, settings):
    url, handler = receiver
    handler.status = 500
    settings.WEBHOOK_MAX_ATTEMPTS = 2
    endpoint = create_webhook(open_event, organizer, url=url)
    delivery = webhooks.ping(endpoint)
    assert delivery.status == WebhookDelivery.Status.FAILED and delivery.next_attempt_at > delivery.created_at
    webhooks.deliver(delivery)
    assert delivery.status == WebhookDelivery.Status.DEAD


def test_private_targets_refused_by_default(open_event, organizer, settings):
    settings.WEBHOOK_ALLOW_PRIVATE_TARGETS = False
    endpoint = create_webhook(open_event, organizer, url="http://127.0.0.1:9/hook")
    delivery = webhooks.ping(endpoint)
    assert delivery.status != WebhookDelivery.Status.SUCCEEDED
    assert "private address" in delivery.last_error


def test_replayed_signature_rejected():
    body = b'{"a":1}'
    header = webhooks.sign("whsec_x", 1_000_000, body)
    assert webhooks.verify_signature("whsec_x", header, body, now=1_000_100)
    assert not webhooks.verify_signature("whsec_x", header, body, now=1_000_000 + 3600)


def test_record_signature_and_tamper_detection(event):
    record = records.sign("certificate", event, {"statement": "hello"})
    assert records.verify(record)
    document = records.export(record)
    assert records.verify_document(document) == (True, "valid")
    document["payload"]["statement"] = "goodbye"
    assert records.verify_document(document)[0] is False


def test_standalone_verifier_agrees(event, tmp_path, client):
    record = records.sign("certificate", event, {"statement": "checked offline"})
    (tmp_path / "record.json").write_text(json.dumps(records.export(record)), encoding="utf-8")
    (tmp_path / "keys.json").write_text(json.dumps(records.jwks()), encoding="utf-8")
    ok = subprocess.run([sys.executable, str(REPO / "tools" / "verify_record.py"), str(tmp_path / "record.json"),  # noqa: S603
                         "--jwks", str(tmp_path / "keys.json")], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout
    doc = records.export(record)
    doc["payload"]["statement"] = "forged"
    (tmp_path / "forged.json").write_text(json.dumps(doc), encoding="utf-8")
    bad = subprocess.run([sys.executable, str(REPO / "tools" / "verify_record.py"), str(tmp_path / "forged.json"),  # noqa: S603
                          "--jwks", str(tmp_path / "keys.json")], capture_output=True, text=True)
    assert bad.returncode == 1


def test_keys_endpoint_publishes_jwk(web, event):
    records.active_key()
    keys = web().get("/.well-known/bananahat-keys.json").json()["keys"]
    assert keys[0]["kty"] == "OKP" and keys[0]["crv"] == "Ed25519"


def test_embed_is_frameable_and_the_rest_is_not(web):
    embed = web().get("/embed/sample-hack-2026/")
    assert embed.status_code == 200
    assert "frame-ancestors *" in embed["Content-Security-Policy"]
    assert "X-Frame-Options" not in embed
    page = web().get("/projects/")
    assert "frame-ancestors 'none'" in page["Content-Security-Policy"]
    assert page["X-Frame-Options"] == "DENY"


def test_openapi_schema_is_served(web):
    schema = web().get("/api/schema/?format=json")
    assert schema.status_code == 200
    paths = json.loads(schema.content)["paths"]
    assert "/api/v1/judges/{judge_id}/scores" in paths
    assert "/api/v1/events/{event_id}/projects" in paths
