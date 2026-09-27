import hashlib
import secrets

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower

from core import clock


class UserManager(BaseUserManager):
    use_in_migrations = True

    def normalize_email(self, email):
        return (email or "").strip().lower()

    def create_user(self, email, name="", password=None, **extra):
        if not email:
            raise ValueError("Users need an email address")
        user = self.model(email=self.normalize_email(email), name=name, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, name="", password=None, **extra):
        extra.update(is_staff=True, is_superuser=True, can_create_events=True)
        return self.create_user(email, name, password, **extra)

    def get_by_natural_key(self, username):
        return self.get(email=self.normalize_email(username))


class User(AbstractBaseUser, PermissionsMixin):
    """A person. Everything they may do inside an event comes from `events.EventRole`.

    The five roles of the brief map onto the schema like this:

    - visitor      not signed in; no row at all
    - participant  EventRole(role=participant), per event
    - judge        EventRole(role=judge), per event
    - organizer    EventRole(role=organizer), per event
    - admin        is_staff, platform-wide: runs the instance, not a particular event

    Roles are per event because people are: the judge at one hackathon competes in the
    next. A global "is_judge" flag cannot say that.
    """

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False, help_text="Platform administrator: can use /admin and see every event."
    )
    can_create_events = models.BooleanField(
        default=False, help_text="May create new events, becoming their first organizer."
    )
    date_joined = models.DateTimeField(default=clock.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="user_email_ci_unique"),
        ]

    def __str__(self):
        return self.name or self.email

    @property
    def display_name(self):
        return self.name or self.email.split("@")[0]

    @property
    def may_create_events(self):
        return self.is_active and (self.is_staff or self.can_create_events)


class ApiTokenQuerySet(models.QuerySet):
    def usable(self):
        now = clock.now()
        return self.filter(revoked_at__isnull=True).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
        )


class ApiToken(models.Model):
    """A bearer token for the REST API.

    Only a SHA-256 digest is stored. The token is shown once, when it is created; a
    database dump does not contain a single usable credential. The short prefix is kept
    in clear so a user can tell their tokens apart.
    """

    PREFIX = "bh_"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=80)
    prefix = models.CharField(max_length=16)
    digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=clock.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = ApiTokenQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @staticmethod
    def hash(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, user, name, *, raw=None, expires_at=None):
        """Create a token and return (token_row, raw_secret). The secret is not kept."""
        raw = raw or cls.PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            name=name,
            prefix=raw[:10],
            digest=cls.hash(raw),
            expires_at=expires_at,
        )
        return token, raw

    @property
    def is_usable(self):
        if self.revoked_at:
            return False
        return not (self.expires_at and self.expires_at <= clock.now())
