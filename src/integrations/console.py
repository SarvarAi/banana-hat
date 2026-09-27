"""Organizer operations on webhooks."""

from django.db import transaction

from audit import services as audit
from events import access
from integrations.models import WebhookEndpoint


def create_webhook(event, user, *, url, description="", event_types=None, is_active=True):
    access.require_organizer(user, event)
    with transaction.atomic():
        endpoint = WebhookEndpoint.objects.create(
            event=event, url=url, description=description, event_types=event_types or [],
            is_active=is_active, created_by=user,
        )
        audit.record("webhook.created", f"{user.display_name} added a webhook to {url}", actor=user,
                     event=event, target=endpoint)
    return endpoint


def delete_webhook(endpoint, user):
    access.require_organizer(user, endpoint.event)
    with transaction.atomic():
        audit.record("webhook.deleted", f"{user.display_name} removed the webhook to {endpoint.url}",
                     actor=user, event=endpoint.event, target=endpoint)
        endpoint.delete()
