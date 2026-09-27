"""Settings for Banana Hat.

Everything an operator might change is read from the environment, so the same image
runs a laptop demo and a production event. There is no settings_prod.py to drift out of
sync: a deployment is this file plus environment variables. The full list is in
README.md, "Configuration".
"""

import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent  # src/
REPO_DIR = BASE_DIR.parent


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def database_from_url(url):
    """Translate a DATABASE_URL into a Django DATABASES entry.

    Two schemes, because those are the two deployments we support: postgres:// for
    anything real, sqlite:/// for a single-container install or a quick look.
    """
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        # sqlite:///relative.db and sqlite:////absolute/path.db
        path = unquote(parsed.path)[1:] or ":memory:"
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": path}
    if parsed.scheme in {"postgres", "postgresql"}:
        options = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or ""),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": options,
        }
    raise ImproperlyConfigured(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


DATA_DIR = Path(env("DATA_DIR", str(REPO_DIR / "var")))


def load_secret_key():
    """SECRET_KEY from the environment, or a key generated once and kept in DATA_DIR.

    A hard-coded fallback key would be shared by every install that forgot to set one,
    which is worse than no key at all. Generating it on first boot means `docker compose
    up` needs no configuration and still gets a secret nobody else has.
    """
    if key := env("SECRET_KEY"):
        return key
    path = DATA_DIR / "secret_key"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(50)
        path.write_text(key)
        path.chmod(0o600)
        return key


SECRET_KEY = load_secret_key()
DEBUG = env_bool("DEBUG", False)

# Demo mode seeds the fixture event, prints fixed test credentials and shows a one-click
# account switcher on the login page. It exists so a judge can evaluate every role in a
# minute. It must never be on for a real event, and the UI says so on every page.
DEMO_MODE = env_bool("DEMO_MODE", False)

SITE_NAME = env("SITE_NAME", "Banana Hat")
SITE_URL = env("SITE_URL", "http://localhost:8080").rstrip("/")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", SITE_URL)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "core",
    "accounts",
    "audit",
    "events",
    "judging",
    "voting",
    "records",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.AccessPolicyMiddleware",
]

ROOT_URLCONF = "bananahat.urls"
WSGI_APPLICATION = "bananahat.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site",
            ],
        },
    },
]

DATABASES = {
    "default": database_from_url(env("DATABASE_URL", f"sqlite:///{REPO_DIR / 'db.sqlite3'}"))
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The database cache is shared by every web worker, which is what DRF's throttles need
# to count correctly. The table is created by `manage.py createcachetable` at boot.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "bananahat_cache",
    }
}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"

SESSION_COOKIE_NAME = "bh_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", False)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_HTTPONLY = True

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if env_bool("BEHIND_TLS_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

FORMS_URLFIELD_ASSUME_HTTPS = True

LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = REPO_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
if DEBUG:
    STORAGES["staticfiles"] = {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    }

# Mail goes to SMTP when configured. docker-compose points it at Mailpit, a local mail
# catcher, so invitations and ballot links are readable at http://localhost:8025 with
# the network off. Unset, mail is written to the log.
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "25"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", False)
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", f"{SITE_NAME} <noreply@localhost>")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "accounts.authentication.BearerTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Default deny. An endpoint that forgets to declare who may call it refuses
    # everybody, instead of quietly serving everybody. tests/test_access_matrix.py
    # additionally fails the build if any API view relies on this default.
    "DEFAULT_PERMISSION_CLASSES": ["core.permissions.DenyAll"],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("THROTTLE_ANON", "120/min"),
        "user": env("THROTTLE_USER", "600/min"),
    },
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "EXCEPTION_HANDLER": "core.api.exception_handler",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Banana Hat API",
    "DESCRIPTION": (
        "Hackathon submission and judging. Authenticate with "
        "`Authorization: Bearer <token>`; create tokens on your account page."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}

# Webhook deliveries are made by `manage.py run_worker`. These bound what a slow or
# hostile receiver can cost us.
WEBHOOK_TIMEOUT_SECONDS = float(env("WEBHOOK_TIMEOUT_SECONDS", "5"))
WEBHOOK_MAX_ATTEMPTS = int(env("WEBHOOK_MAX_ATTEMPTS", "8"))
# Receivers on private addresses are refused unless explicitly allowed, so an organizer
# cannot point a webhook at the database container. Set to true for local testing.
WEBHOOK_ALLOW_PRIVATE_TARGETS = env_bool("WEBHOOK_ALLOW_PRIVATE_TARGETS", False)
