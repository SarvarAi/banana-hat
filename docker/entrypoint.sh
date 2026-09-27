#!/bin/sh
# Container entry point.
#
#   web      migrate, seed if asked, then serve with gunicorn
#   worker   deliver webhooks and run housekeeping
#   <other>  run as a command, e.g. `docker compose run --rm web python src/manage.py shell`
set -eu

cd /app/src

case "${1:-web}" in
  web)
    python manage.py migrate --noinput
    python manage.py createcachetable
    if [ "${SEED_FIXTURES:-false}" = "true" ]; then
      python manage.py seed --fixtures /app/fixtures.json
    fi
    exec gunicorn bananahat.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers "${WEB_CONCURRENCY:-3}" \
      --threads "${WEB_THREADS:-4}" \
      --access-logfile - \
      --no-control-socket \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
    ;;
  worker)
    exec python manage.py run_worker
    ;;
  *)
    exec "$@"
    ;;
esac
