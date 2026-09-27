"""Make a database that was once a demo safe to run a real event on.

Revokes every demo token and replaces every demo password that is still the published
one with an unusable password. The container runs this on every boot when DEMO_MODE is
off, so following the "PROD" lines in docker-compose.yml is enough even if the volume
was first created in demo mode. Afterwards, set a real admin password with
`python manage.py changepassword admin@bananahat.localhost` or create a new superuser.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts import demo
from accounts.models import ApiToken, User
from audit import services as audit
from core import clock


class Command(BaseCommand):
    help = "Revoke demo tokens and scrub demo passwords (run automatically when DEMO_MODE is off)."

    def handle(self, *args, **options):
        digests = [ApiToken.hash(raw) for raw in demo.TOKENS.values()]
        with transaction.atomic():
            tokens = ApiToken.objects.filter(digest__in=digests, revoked_at__isnull=True)
            revoked = tokens.update(revoked_at=clock.now())
            scrubbed = 0
            # Only the five demo accounts: checking every user against slow password
            # hashes would make boot take minutes on a real event.
            for account in demo.accounts():
                user = User.objects.get(email=account["email"])
                if user.has_usable_password() and user.check_password(demo.PASSWORDS[account["key"]]):
                    user.set_unusable_password()
                    user.save(update_fields=["password"])
                    scrubbed += 1
            if revoked or scrubbed:
                audit.record("demo.disarmed",
                             f"Demo mode is off: revoked {revoked} demo token(s), scrubbed {scrubbed} demo password(s)",
                             source="system")
        if revoked or scrubbed:
            self.stdout.write(f"demo mode is off: revoked {revoked} demo token(s), scrubbed {scrubbed} password(s)")
