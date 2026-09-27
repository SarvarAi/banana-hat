"""Shared fixtures.

The fixture event is loaded once per test session, the same way `docker compose up`
loads it (the seed command, in demo mode). Every test then runs inside a transaction
that is rolled back, so tests can mutate freely without seeing each other.
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import Client

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures.json"


@pytest.fixture(scope="session", autouse=True)
def _test_settings():
    from django.conf import settings

    settings.STORAGES = {**settings.STORAGES, "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}
    settings.ALLOWED_HOSTS = ["*"]


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    from django.conf import settings

    settings.DEMO_MODE = True
    with django_db_blocker.unblock():
        call_command("seed", fixtures=str(FIXTURES), quiet=True, verbosity=0)
    settings.DEMO_MODE = False


@pytest.fixture
def fixture_data():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


@pytest.fixture
def event(db):
    from events.models import Event

    return Event.objects.get(public_id="evt_01")


@pytest.fixture
def open_event(db):
    from events.models import Event

    return Event.objects.get(slug="open-jam")


def _user(email):
    from accounts.models import User

    return User.objects.get(email=email)


@pytest.fixture
def organizer(db):
    from accounts import demo

    return _user(demo.ORGANIZER_EMAIL)


@pytest.fixture
def admin_user(db):
    from accounts import demo

    return _user(demo.ADMIN_EMAIL)


@pytest.fixture
def judge_a(db):
    from events.models import EventRole

    return EventRole.objects.get(public_id="jdg_24").user


@pytest.fixture
def judge_b(db):
    from events.models import EventRole

    return EventRole.objects.get(public_id="jdg_26").user


@pytest.fixture
def participant(db):
    from events.models import Team

    return Team.objects.get(public_id="tm_01").memberships.get(is_captain=True).user


@pytest.fixture
def stranger(db):
    from accounts.models import User
    from core import clock

    # An established account: brand-new accounts are held for review when they vote.
    return User.objects.create_user(email="stranger@example.net", name="Sam Stranger", password="x" * 12,
                                    date_joined=clock.now() - timedelta(days=30))


class Api:
    """A test client that talks to the API with a bearer token, like the checker."""

    def __init__(self, token=None):
        self.client = Client()
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def get(self, path, **params):
        return self.client.get(path, params, **self.headers)

    def post(self, path, data=None):
        return self.client.post(path, json.dumps(data or {}), content_type="application/json", **self.headers)

    def put(self, path, data=None):
        return self.client.put(path, json.dumps(data or {}), content_type="application/json", **self.headers)

    def patch(self, path, data=None):
        return self.client.patch(path, json.dumps(data or {}), content_type="application/json", **self.headers)

    def delete(self, path):
        return self.client.delete(path, **self.headers)


@pytest.fixture
def api(db):
    """api("organizer") returns a client using that demo role's token; api() is anonymous."""
    from accounts import demo

    def make(role=None):
        return Api(demo.TOKENS[role] if role else None)

    return make


@pytest.fixture
def token_for(db):
    from accounts.models import ApiToken

    def make(user):
        _, raw = ApiToken.issue(user, "test")
        return Api(raw)

    return make


@pytest.fixture
def web(db):
    """web(user) returns a logged-in HTML client."""

    def make(user=None):
        client = Client()
        if user is not None:
            client.force_login(user)
        return client

    return make


@pytest.fixture
def clock(monkeypatch):
    """Move the application's clock: clock.set(dt) or clock.shift(hours=3)."""
    from core import clock as app_clock

    real_now = app_clock.now

    class Clock:
        offset = timedelta()

        def set(self, when):
            self.offset = when - real_now()

        def shift(self, **kwargs):
            self.offset += timedelta(**kwargs)

    c = Clock()
    monkeypatch.setattr(app_clock, "now", lambda: real_now() + c.offset)
    return c
