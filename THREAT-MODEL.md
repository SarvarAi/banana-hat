# Threat model

Who might cheat a hackathon, how, what Banana Hat does about it, and what it does not.
The organisers asked for "abuse considered up front"; this is that consideration. Each
defence names the code and the test that holds it.

## What we are protecting

1. **The ranking**: it decides prizes. It should reflect what the judges thought, not who
   gamed the system.
2. **The deadline**: fairness between teams that stopped at 18:00 and teams that did not.
3. **Judges' independence**: a judge who sees peers' scores anchors on them, and a
   participant who sees a judge's scores can pressure that judge.
4. **The community vote**: the easiest thing to stuff.
5. **The record**: after the event, "who changed what, and when" must have a trustworthy
   answer.

## Actors

| Actor | Can | Wants |
|---|---|---|
| Visitor | browse the gallery, read published results | a free vote, or many |
| Participant | edit their team's project, vote, comment | a better rank, a later deadline |
| Judge | score their assignments | to align with peers, to help a friend's team |
| Organizer | everything in their event except writing scores | usually honesty; occasionally a favourite |
| Platform admin | the server and the database | we treat them as trusted, and make tampering *evident* |
| Outsider | send HTTP requests | to break in, flood, or use the portal against its own network |

## Threats and defences

### Ballot stuffing and Sybil votes

*One person casts many community votes.*

- **Every ballot costs an identity.** The organizer picks one of three modes per event:
  a signed-in account with a **confirmed** email address, a verified email inbox, or a
  single-use printed link. There is no anonymous voting mode. An account whose address
  was never confirmed cannot vote: an unconfirmed account costs nothing to create, so it
  is not an identity. Proof of the inbox is any link we emailed and the user opened: the
  sign-up confirmation, a completed password reset, or an accepted judge invitation.
  (`voting/services.py`, `accounts/verification.py`)
- **Voters can be limited to email domains** per event (`voter_email_domains`, e.g. a
  university's domain). It applies to confirmed addresses in signed-in mode and to
  verified inboxes in email mode, so it cannot be claimed without receiving mail there.
- **One identity, one ballot, in the database.** `(event, identity_digest)` is unique
  (`one_ballot_per_voter`). The digest is HMAC-SHA256 of the identity with `SECRET_KEY`,
  per event, so the table holds no emails and cannot be reversed by guessing.
- **Email aliases fold together.** `Jane.Doe+vote@googlemail.com` and
  `janedoe@gmail.com` are one voter: we lower-case, strip `+tags`, and for Gmail remove
  dots and unify the domain. (`canonical_email`, `test_email_aliases_fold_into_one_identity`)
- **Suspicious ballots are held, not counted, and never silently dropped.** At cast time
  a ballot is flagged when:
  - 6 or more ballots came from one /24 (IPv4) or /48 (IPv6) network within 10 minutes,
  - 3 or more came from the same browser and address,
  - the voting account was created minutes before voting (the cheapest Sybil),
  - the email domain is a known disposable-mail provider.

  A flagged ballot does not count until an organizer reviews it and chooses **Count** or
  **Void**, and that decision is audited. An office full of people on one Wi-Fi network
  is normal, which is why these are signals for a human and not automatic rejections.
  (`assess`, `test_burst_from_one_network_is_held_not_counted`,
  `test_brand_new_account_is_held_for_review`)
- **Rate limits** on ballot changes, email verification requests, sign-up, sign-in,
  password resets and comments, counted in the database so they hold across web
  workers, plus DRF throttles on the whole API. Addresses are stored only as digests.
- **No running score to chase.** Vote totals are refused (403 `results_hidden`) to
  everyone, organizers and admins included, until voting closes. Campaigners get no
  target to aim at, and late tactical voting gets nothing to react to.
  (`test_tally_hidden_from_everyone_while_open`)
- **No position bias.** Each ballot shows projects in its own random order, stable for
  that ballot, so the project at the top of the list does not get free votes.
  (`test_order_is_random_per_ballot_and_stable`)
- **No self-voting**, in signed-in and email modes. An email ballot is linked to the
  account that owns that mailbox (matching the canonical address), so a participant
  cannot vote for their own team from a private window either. Printed ballot links are
  anonymous by design, so link mode cannot know who is voting; hand them out at the
  venue, not to teams.
- **Held ballots are decided while voting is open.** Once voting closes, the totals are
  public, so reviewing a held ballot after that would let an organizer pick the outcome;
  it is refused. The voting window and settings are also frozen once voting has closed,
  so an organizer cannot close it for a moment, read the totals, and reopen it. Closing
  early is allowed; it is just final.

**Residual risk, stated plainly.** Someone with fifty real email inboxes, spread across
networks, can cast fifty votes. No self-hosted system without identity verification can
stop that, and we do not pretend to. Restricting voters to a known domain, or handing out
printed links at the venue, raises the cost further. Organizers should weigh the
community vote accordingly; by default it decides a separate "people's choice" prize,
not the judged ranking.

### Judge collusion and anchoring

*Judges coordinate, or a judge favours a team.*

- **Judges cannot see each other's scores**, over any endpoint, enforced by scoped
  querysets and explicit 403s ([JUDGING.md §9](JUDGING.md#9-isolation-what-a-judge-can-and-cannot-see)).
  They cannot see rankings before publication. There is nothing to anchor on.
- **Conflicts of interest are first-class.** Judges recuse with a reason; the score is
  voided, the team is blocked from ever being assigned to them again, and the organizer
  sees it. Recusal obeys the judging window like scoring does, so it cannot be used to
  strike a score after scores have locked or results are published. A judge cannot be a
  participant or organizer in the event they judge, and an organizer cannot compete.
- **Normalization blunts a single outlier.** A judge who scores everyone 5 is corrected
  for their generosity; one who scores everyone the same is flagged on the dashboard.
- **Detection, not just prevention.** The dashboard shows each judge's mean, spread and
  estimated bias, and flags flat scorers. The audit trail records every score change
  with before and after values, so "judge X raised team Y's score at 03:12, after the
  deadline" is findable.
- **Varied pairings.** Automatic assignment prefers judges who have reviewed least
  together, which dilutes a clique and keeps bias estimates well conditioned.

**Residual risk.** Two judges who agree out of band to over-score one project can move
it. With three reviews per project that is a majority. Normalization cannot tell honest
enthusiasm from collusion. What it can do is leave evidence: both scores and their
timing are in the audit trail, and any project whose rank depends on two reviewers shows
a wide confidence interval.

### Late submissions and deadline games

- One function decides whether a team may edit, and every write path calls it first,
  including the API before it validates the body
  ([ARCHITECTURE.md](ARCHITECTURE.md#deadline-enforcement)). Tests pin the exact second.
- Extensions are per team, granted only by organizers, with a mandatory reason in the
  audit trail. Other teams stay locked.
- **Residual risk:** the deadline is only as right as the server clock. Run NTP.

### Privilege escalation and data leaks

- Default deny for every HTML and API view, checked structurally by a test that walks
  the URL table.
- Roles are per event and checked on the object, never inferred from the URL.
- Drafts are visible only to their team and the organizers; unlisted events 404 for
  outsiders.
- Organizers cannot write scores, only void them with a reason, so an organizer cannot
  quietly fix a result.
- Tokens are stored hashed, shown once, expire and can be revoked. Passwords use
  Django's PBKDF2 with a minimum length and a common-password check. Sessions are
  HttpOnly and SameSite=Lax; forms carry CSRF tokens.
- Password reset and the email ballot answer the same way whether or not the address is
  known, so neither form reveals who is registered or who has voted.

### Tampering with the record

*Someone with database access edits a score or deletes an audit entry after the fact.*

- The audit trail is a hash chain. Editing, deleting or truncating any entry breaks
  verification from that point, and the audit page says where.
  (`test_editing_an_old_entry_breaks_the_chain` and friends)
- At publication, the chain's head digest is included in the **signed** results record.
  Once that record has been downloaded, rewriting history (even the whole chain)
  produces a head that no longer matches the signature.
- Each judge's signed participation record carries a salted commitment to their scores.
  If a judge later disputes what they submitted, revealing the salt settles it.
  Unpublishing results revokes every record of the event (results, certificates, judge
  records), because they describe results that may now change; the next publication
  issues fresh ones. A revoked record says so on its verification page.
- The chain hashes only what an entry says (the actor's name, the event's id as text),
  not database row ids, so routinely deleting a spam account or a test event does not
  look like tampering.
- **Residual risk:** before publication, an attacker with full database access and the
  `SECRET_KEY` could rewrite everything consistently. Platform admins are trusted; the
  design makes their tampering evident after publication, not impossible before it.

### Attacks on the server

| Attack | Defence |
|---|---|
| XSS | Django autoescaping everywhere; a CSP that forbids inline and third-party scripts; no inline JS exists to whitelist. |
| Clickjacking | `X-Frame-Options: DENY` and `frame-ancestors 'none'`, except the read-only gallery widget. |
| CSRF | Django CSRF on every form; bearer tokens are never sent automatically by browsers. |
| SSRF via webhooks | Only globally routable receivers are allowed (Python's `is_global`): private, loopback, link-local, carrier-grade NAT, NAT64 and other special ranges are refused, and redirects are not followed. `WEBHOOK_ALLOW_PRIVATE_TARGETS` exists for local testing only. (Residual: DNS rebinding between our check and the connection. The request carries no credentials and its response is discarded, which limits the damage.) |
| Flooding | DRF throttles (120/min anonymous, 600/min per user, configurable), DB-backed limits on sensitive forms, gunicorn timeouts. `X-Forwarded-For` is believed only when `BEHIND_TLS_PROXY` is set, so a client cannot claim a fresh address per request to dodge the limits. |
| Credential stuffing | Sign-in limited per address and per account; tokens are random and hashed. |
| Malicious import files | The import is validated as a whole, runs in one transaction, and creates no passwords. Only users allowed to create events can import. |
| Webhook secret theft | Secrets are per endpoint and shown only to that event's organizers. They are stored in clear because they must be used to sign; rotate by deleting and re-adding the endpoint. |

## Demo mode

`DEMO_MODE=true` (the default in `docker-compose.yml`) creates fixed, publicly known
tokens and passwords and a one-click account switcher, so a judge can evaluate every
role in a minute. **It is a deliberate hole.** Every page shows a banner while it is on.
Turn it off (and `SEED_FIXTURES`) before running a real event, and the hole closes even
on a database that began as a demo:

- the seed does not create demo credentials and the switcher returns 404;
- bearer authentication refuses any token starting with `bh_demo_`;
- on every boot outside demo mode, the container runs `manage.py disarm_demo`, which
  revokes the demo tokens and replaces the published demo passwords with unusable ones
  (then set a real admin password with `manage.py changepassword`).
