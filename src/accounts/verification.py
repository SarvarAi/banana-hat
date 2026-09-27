"""Proving that a user reads the inbox their account is registered with.

Sign-up does not require it; signed-in community voting does. Without it, an account is
a free identity, and "one account, one ballot" would mean nothing. Any link we emailed
and the user opened counts as proof: the verification link itself, a completed password
reset, or an accepted judge invitation.

Tokens are signed with SECRET_KEY and carry the address they were issued for, so
changing the account's email invalidates them. Nothing is stored.
"""

from datetime import timedelta

from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse

from accounts.models import User
from audit import services as audit
from core import clock
from core.errors import DomainError

SALT = "bananahat.email-verification"
MAX_AGE = timedelta(days=3)


def make_token(user) -> str:
    return signing.dumps({"u": user.pk, "e": user.email}, salt=SALT, compress=True)


def mark_verified(user, how: str):
    if user.email_verified_at:
        return
    with transaction.atomic():
        user.email_verified_at = clock.now()
        user.save(update_fields=["email_verified_at"])
        audit.record("account.email_verified", f"{user.display_name} confirmed their email address ({how})",
                     actor=user, target=user)


def verify(token: str) -> User:
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    except signing.SignatureExpired as exc:
        raise DomainError("This link has expired. Send yourself a new one from your account page.",
                          code="link_expired") from exc
    except signing.BadSignature as exc:
        raise DomainError("This link is not valid.", code="invalid_link", status=404) from exc
    user = User.objects.filter(pk=data.get("u"), email=data.get("e"), is_active=True).first()
    if user is None:
        raise DomainError("This link no longer matches an account.", code="invalid_link", status=404)
    mark_verified(user, "verification link")
    return user


def send(user, *, base_url):
    link = base_url + reverse("accounts:verify-email", args=[make_token(user)])
    send_mail(
        subject="Confirm your email address",
        message=(f"Hello {user.display_name},\n\nConfirm that this is your address by opening:\n\n{link}\n\n"
                 "The link works for three days. If you did not create an account, ignore this email."),
        from_email=None, recipient_list=[user.email], fail_silently=False,
    )
