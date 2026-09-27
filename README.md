# Banana Hat

**A self-hosted hackathon portal where deadlines hold, judges never see each other's
scores, and every ranking comes with its math, its audit trail and a signature.**

Teams form by invite link and submit until the deadline, and not a minute after. Judges
score a weighted rubric the organizer controls, and the backend refuses to show them
anyone else's numbers. Harsh and generous judges are evened out by a method documented
and defended in [JUDGING.md](JUDGING.md). The public gets a searchable gallery, comments
and a community vote whose totals stay hidden until it closes. Every decision lands in
a hash-chained audit trail, and published results are signed with Ed25519, so anyone can
check them without trusting the server.

Built for [DOGFOOD 2026](https://dogfoodhack.com). Apache-2.0.

```
docker compose up
```

Then open **http://localhost:8080**.

---

- [Try it in two minutes](#try-it-in-two-minutes)
- [What is in it](#what-is-in-it)
- [Acceptance](#acceptance)
- [Running a real event](#running-a-real-event)
- [Configuration](#configuration)
- [API and webhooks](#api-and-webhooks)
- [Getting data in and out](#getting-data-in-and-out)
- [Development](#development)
- [Honest limitations](#honest-limitations)
- [Documentation](#documentation)

## Try it in two minutes

`docker compose up` builds one image and starts PostgreSQL, the web app, a background
worker and [Mailpit](https://mailpit.axllent.org) (a local mail catcher). The web
container migrates, loads `fixtures.json`, and prints:

```
seeded. test logins:
  organizer    Authorization: Bearer bh_demo_organizer_2026
  judge_a      Authorization: Bearer bh_demo_judge_a_2026
  judge_b      Authorization: Bearer bh_demo_judge_b_2026
  participant  Authorization: Bearer bh_demo_participant_2026
  admin        Authorization: Bearer bh_demo_admin_2026
```

The first run needs the network to pull the base images and Python packages. After
that, everything runs with the network off: no CDN, no web fonts, no external API, no
hosted anything. If port 8080 is taken, use `BANANAHAT_PORT=8090 docker compose up`.

On the sign-in page, the **Demo accounts** panel logs you in as any role with one click:

| Account | Try this |
|---|---|
| **Organizer** (Olu) | *My events → Sample Hack 2026*: the live progress dashboard, judges flagged as flat or lenient, the Results tab comparing four normalization methods, *Publish*. |
| **Judge A** (Diego Herrera, `jdg_24`) | The review queue with pending reviews, the scorecard with a live weighted total, recusal. |
| **Judge B** (Jonas Vogel, `jdg_26`) | The same, and a 403 if you ask the API for Judge A's scores. |
| **Participant** (Priya, team NorthKiln) | Sample Hack is closed, so the submission page is locked. In **Open Jam 2026**, edit the draft, submit, invite teammates, vote, comment. |
| **Admin** | `/admin`, read-only for judging data. |

Two events are seeded:

- **Sample Hack 2026**: `fixtures.json` exactly as published. 41 projects, 30 judges, 126
  scores, submissions closed on 1 March 2026. It includes every awkward case: the flat
  judge, the unfinished batches (topped up with 8 pending reviews), the duplicate
  submission (flagged, not deleted), and three different teams called StillTrail.
- **Open Jam 2026**: a small live event, open for submissions and community voting, for
  trying everything the closed event refuses.

Mail (judge invitations, ballot links, password resets) shows up at
**http://localhost:8025**.

## What is in it

All four tiers of the brief. The official `run.py` checks T1 and T2;
[`tools/check_extended.py`](tools/check_extended.py) checks T3 and T4 against the running
portal in the same style (see [Acceptance](#acceptance)).

**T1: core**
- Sign-up, sign-in, sessions, password reset (which doubles as "claim your account" for
  people imported from a fixture), API tokens.
- Roles per event: participant, judge and organizer are rows in `EventRole`; admin is
  platform-wide; visitors are anyone signed out. A judge cannot compete or organize in
  the same event.
- Events with configurable dates, tracks and prizes; a starter rubric; drafts until listed.
- Teams by invite link: rotate the link to revoke it, size limit, captain.
- Projects: save a draft, submit, edit until the deadline, back to draft, withdraw.
- **The deadline holds**: one function decides, every write path (HTML, API, organizer
  console) calls it first, and tests pin the exact second. Per-team extensions are
  organizer-only and audited.
- A public gallery with search, event and track filters, and sorting.

**T2: judging**
- Judge invitations by email; judge tracks; conflicts of interest and recusal.
- Automatic assignment (most constrained first, balanced load, varied pairings,
  deterministic), plus manual assign and unassign.
- A weighted rubric the organizer can reweight at any time without re-scoring.
- **Role isolation in the backend**: scoped querysets, explicit 403s, default-deny
  permissions checked by a test over every URL.
- A live progress dashboard: per-track progress, per-judge mean, spread and estimated
  bias, flags for flat scorers and single-review judges.
- **Normalization**: a ridge-regularised judge-effect model with empirical-Bayes
  shrinkage by default; z-scores, raw means and Bradley–Terry on each judge's ranking
  also available. Each has confidence intervals, statistical-tie warnings, and a
  simulation study on the fixture's own assignment graph ([JUDGING.md](JUDGING.md)).
- CSV export of scores, results and projects, plus the audit trail.

**T3: public**
- Community voting in three modes: signed-in with a confirmed email, verified email, or
  single-use ballot links; optionally limited to email domains.
- Comments, with organizer moderation (hide with a reason, never delete).
- Vote totals hidden from everyone, organizers included, until voting closes.
- Each ballot has its own random, stable project order.
- Anti-abuse: DB-backed rate limits, one ballot per identity as a database constraint,
  email alias folding, no voting for your own team, suspicious ballots held for review
  (and decided before the totals are public), voting settings frozen once voting closes,
  and a hash-chained audit trail that organizers can read, filter and download
  ([THREAT-MODEL.md](THREAT-MODEL.md)).

**T4: stretch**
- A REST API (`/api/v1/`) with an OpenAPI 3 document: committed at
  [`docs/openapi.yaml`](docs/openapi.yaml) (a test fails if it drifts from the code) and
  browsable in Swagger UI at `/api/docs/`, served offline.
- Webhooks: HMAC-signed, a transactional outbox, retries with backoff, SSRF protection.
- Signed Ed25519 records for published results, judge participation (with a commitment
  to their scores) and participant certificates, verifiable offline with
  [`tools/verify_record.py`](tools/verify_record.py) (standard library only).
- Printable certificates.
- An embeddable gallery widget: an iframe, or a one-line script that auto-sizes it.
- Bulk import and export: whole events in the DOGFOOD fixture format (round-trip
  tested), judge rosters from CSV (`email,name,tracks`, all-or-nothing validation), and
  CSV exports of scores, results, projects and the audit trail.
- Pairwise judging mode (Bradley–Terry), the Pairwise bonus challenge.

## Acceptance

```
python3 run.py .dogfood.toml > acceptance-report.txt
python3 tools/check_extended.py .dogfood.toml > acceptance-report-extended.txt
```

Both reports are committed, generated against a fresh `docker compose up` (new image,
empty volumes): [acceptance-report.txt](acceptance-report.txt) (T1 and T2: 7 of 7 pass)
and [acceptance-report-extended.txt](acceptance-report-extended.txt) (T3 and T4: 12 of 12
pass). They say `localhost:8090` because port 8080 was taken on the machine that
generated them, so the stack ran with `BANANAHAT_PORT=8090` and `base_url` was changed to
match. Nothing else differs.

**A note on the official report.** `run.py` has checks only for T1 and T2, so for any
portal claiming T3 or T4 it prints `note: claimed but not verified: T3 T4`. That line
describes the checker, not a failure. The extended report is our evidence for those
tiers, and each item above names where it lives. The tests in `tests/` also run the
unmodified `run.py` against a live server on every test run, so a regression in T1 or
T2 fails the build.

## Running a real event

1. Copy `docker-compose.yml` and change the lines marked `PROD`:
   `DEMO_MODE=false`, `SEED_FIXTURES=false`, `WEBHOOK_ALLOW_PRIVATE_TARGETS=false`, a real
   database password, your `SITE_URL` and `ALLOWED_HOSTS`, and your SMTP server in
   `EMAIL_HOST` and friends (drop the Mailpit service).
2. Put it behind a TLS-terminating proxy (Caddy, nginx, Traefik) and set
   `BEHIND_TLS_PROXY=true` and `SECURE_COOKIES=true`.
3. Create yourself an admin account:
   `docker compose run --rm web python manage.py createsuperuser`.
   If the volume started as a demo, you do not have to wipe it: with `DEMO_MODE` off,
   every boot runs `manage.py disarm_demo`, which revokes the published demo tokens and
   scrubs the demo passwords.
4. Sign in, create the event (or import one on the console's Import page), add tracks,
   prizes and the rubric, list it, and share the link. Invite judges from the Judges tab.
5. After the deadline: *Assignments → Assign*, watch *Live progress*, look at *Results*,
   and *Publish* when the intervals say the order is real.

Back up the database with `pg_dump`, **and** keep `SECRET_KEY` (or the `app-data`
volume that holds the generated one): the signing key is encrypted with it.

## Configuration

Everything is an environment variable. There is one settings file for every environment.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | SQLite file in the repo | `postgres://user:pass@host:5432/db` or `sqlite:////data/db.sqlite3` |
| `SECRET_KEY` | generated once into `DATA_DIR/secret_key` | Signs sessions; encrypts the signing key. Never shared between installs. |
| `DATA_DIR` | `/data` in the image | Where the generated secret lives. |
| `SITE_URL` | `http://localhost:8080` | Used in emails, records and certificates. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated host names. |
| `CSRF_TRUSTED_ORIGINS` | `SITE_URL` | For HTTPS behind a proxy. |
| `DEMO_MODE` | `false` | Demo credentials, account switcher, banner. **Off for real events.** |
| `SEED_FIXTURES` | `false` | Load `fixtures.json` on start (idempotent). |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | log to console | SMTP settings. |
| `SECURE_COOKIES` | `false` | Secure-only cookies; set with HTTPS. |
| `BEHIND_TLS_PROXY` | `false` | Trust `X-Forwarded-Proto` from the proxy. |
| `THROTTLE_ANON`, `THROTTLE_USER` | `120/min`, `600/min` | API rate limits. |
| `WEBHOOK_TIMEOUT_SECONDS`, `WEBHOOK_MAX_ATTEMPTS` | `5`, `8` | Webhook delivery limits. |
| `WEBHOOK_ALLOW_PRIVATE_TARGETS` | `false` | Allow receivers on private addresses (local testing only). |
| `WEB_CONCURRENCY`, `WEB_THREADS` | `3`, `4` | Gunicorn processes and threads. |
| `BANANAHAT_PORT` | `8080` | Host port in `docker-compose.yml`. |
| `SITE_NAME`, `LOG_LEVEL`, `DEBUG` | | The obvious. |

## API and webhooks

Create a token on your account page, then:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/judges/me/scores
curl -H "Authorization: Bearer $TOKEN" -X PUT http://localhost:8080/api/v1/assignments/asg_.../score \
     -H "Content-Type: application/json" -d '{"values": {"functionality": 4, "quality": 3, "innovation": 5}}'
curl http://localhost:8080/api/v1/events/evt_01/results          # 403 until published
curl -H "Authorization: Bearer $TOKEN" -OJ http://localhost:8080/api/v1/events/evt_01/exports/event.json
```

The OpenAPI document is committed at [`docs/openapi.yaml`](docs/openapi.yaml), served live
at `/api/schema/`, and browsable at `/api/docs/`.
Errors always look like `{"error": {"code": "submissions_closed", "message": "..."}}`.

A webhook receiver, verifying the signature and rejecting replays:

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(","))
    if abs(time.time() - int(parts["t"])) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])

# verify(SECRET, request.headers["X-BananaHat-Signature"], request.body)
```

## Getting data in and out

- **In:** `python manage.py seed --fixtures file.json`, the console's Import page, or
  `POST /api/v1/events/import`, all taking the DOGFOOD fixture format. Validation
  reports every problem at once; the import is one transaction. Judge rosters come in
  as CSV on the Judges tab or `POST /api/v1/events/{id}/judges/invitations`.
- **Out:** `event.json` (fixture format plus weights, prizes and pending assignments,
  losslessly re-importable), `scores.csv`, `results.csv`, `projects.csv`, `audit.csv`.
- **Whole database:** `pg_dump`. See [DATA-MODEL.md](DATA-MODEL.md#import-and-export).

## Development

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cd src && python manage.py migrate && DEMO_MODE=true python manage.py seed
DEMO_MODE=true DEBUG=true python manage.py runserver 8080
```

Tests (148 of them, about 25 seconds, SQLite by default):

```bash
pytest
DATABASE_URL=postgres://user:pass@localhost:5432/test pytest    # against PostgreSQL
```

They include the official `run.py` against a live server, a role × endpoint access
matrix, a structural check that every view declares its access, deadline tests at
second precision, import/export round trips against the real `fixtures.json`, audit
tamper detection, the statistical claims in JUDGING.md as assertions, and a regression
test for each finding of an independent security review (`tests/test_hardening.py`).
`ruff check` keeps the style.

Reproduce the normalization study:
`docker compose exec web python src/manage.py normalization_report --runs 1000`.

## Honest limitations

- **Sybil voting is raised in cost, not solved.** Someone with many real inboxes can
  vote many times. We require a confirmed address, fold aliases, hold suspicious ballots
  and say so ([THREAT-MODEL.md](THREAT-MODEL.md)).
- **Printed ballot links are anonymous**, so in link mode we cannot stop someone voting
  for their own team. Signed-in and email modes do stop it.
- **Normalization corrects a judge's level, not their spread**, and treats 1–5 scores as
  interval data. On the fixture's own design this costs at most 0.002 in rank
  correlation against the best alternative ([JUDGING.md §11](JUDGING.md#11-limitations)).
- **Projects are links, not uploads.** There is no file storage, so demo videos and
  slides are URLs. That is deliberate (no object store to run), and it is a limitation.
- **Times are shown in UTC.** Events store a timezone, but the UI does not convert yet.
- **English only.** No i18n.
- **Webhook secrets are stored in clear**, because they must be used to sign. They are
  visible only to that event's organizers.
- **The audit chain is tamper-evident, not tamper-proof**, until results are published
  and the chain head is signed.
- **Accessibility**: semantic HTML, labels, visible focus, reduced motion respected,
  keyboard-operable scorecard. Not yet audited with a screen reader.
- **Scale**: exercised at fixture scale (41 projects, 30 judges) and in tests; not load
  tested beyond that. Results are computed per request (about 25 ms for the fixture).
- **Demo mode is a deliberate hole** (public credentials). It says so on every page, and
  it closes when `DEMO_MODE` is turned off.

## Documentation

| Document | What is in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design, request decisions, deadline enforcement, audit chain, outbox, signing, and the decisions we rejected. |
| [DATA-MODEL.md](DATA-MODEL.md) | Every table, every constraint and why, and the import/export paths. |
| [JUDGING.md](JUDGING.md) | Assignment, scoring, the normalization method with its proof on the fixture data, pairwise mode, isolation. |
| [THREAT-MODEL.md](THREAT-MODEL.md) | Sybil votes, ballot stuffing, collusion, tampering, and what we do not stop. |
| [DEMO.md](DEMO.md) | The five-minute demo: one event lifecycle, create to publish. |
| [docs/openapi.yaml](docs/openapi.yaml) | The REST API, as an OpenAPI 3 document. |

## License

Apache License 2.0; see [LICENSE](LICENSE). `run.py` and `fixtures.json` are the DOGFOOD
organisers' files, included unchanged so the acceptance check can be reproduced.
