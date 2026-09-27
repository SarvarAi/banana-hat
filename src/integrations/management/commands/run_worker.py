"""The background worker: delivers webhooks and does housekeeping.

    python manage.py run_worker            # loop forever (what docker compose runs)
    python manage.py run_worker --once     # one pass, e.g. from cron
"""

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from core import ratelimit
from integrations import webhooks

log = logging.getLogger("bananahat.worker")


class Command(BaseCommand):
    help = "Deliver webhooks and purge expired rate-limit counters."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=2.0)

    def handle(self, *args, once, interval, **options):
        stopping = {"flag": False}

        def stop(signum, frame):
            stopping["flag"] = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        log.info("worker started")
        last_purge = 0.0
        while not stopping["flag"]:
            close_old_connections()
            try:
                sent = webhooks.deliver_due()
                if sent:
                    log.info("processed %d webhook deliveries", sent)
                if time.monotonic() - last_purge > 3600:
                    purged = ratelimit.purge_expired()
                    last_purge = time.monotonic()
                    if purged:
                        log.info("purged %d expired rate-limit counters", purged)
            except Exception:
                log.exception("worker pass failed; retrying")
            if once:
                break
            time.sleep(interval)
        log.info("worker stopped")
