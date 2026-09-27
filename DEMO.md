# Five-minute demo

One full event lifecycle (create, submit, judge, publish) on a fresh event, then a
minute on what makes the judging trustworthy, using the fixture event. Written as a
script for the video; every step is a click path you can follow.

**Setup.** `docker compose up`, then open http://localhost:8080 in one browser window
and http://localhost:8025 (Mailpit) in a tab. Use the **Demo accounts** panel on the
sign-in page to switch roles; *Sign out* in the top right, then pick the next account.

---

## 0:00 Create the event (Organizer)

1. Sign in as **Organizer**. *My events → Create an event*.
2. Name **Demo Day**. Submissions open an hour ago, close in an hour (times are UTC).
   Leave the rest.
   *Create event*. It starts as a draft with a starter rubric (Functionality ×2,
   Quality ×1, Innovation ×1).
3. On the Overview tab: add a track, **Tools**. Add a prize, **Best in show**. Tick
   *Listed publicly*, *Save settings*.

> Say: dates, tracks, prizes and the rubric are all the organizer's. Phases are never
> stored; "is it open?" is computed from the clock on every request.

## 0:45 Form a team and submit (Participant)

1. Sign out, sign in as **Participant**. Open **Demo Day** from *Events*.
2. *Create or join a team* → **Night Owls** → the team page shows the invite link.
3. *Go to your submission*: title **Owl Lamp**, track Tools, a summary.
   *Save draft*: the stamp says DRAFT and it is not in the gallery. *Save and submit*:
   SUBMITTED, with the deadline countdown.

## 1:30 Invite a judge (Organizer → Judge A)

1. Organizer: *Judges* tab → invite `diego.herrera@example.org`.
2. Mailpit tab: open the invitation, copy the link.
3. Sign in as **Judge A**, paste the link, *Accept and become a judge*.

## 2:00 The deadline holds

1. Organizer: Overview → set *Submissions close* to a minute ago → *Save settings*.
   (The audit trail marks this change "affects judging".)
2. Participant: the submission page is now locked: CLOSED stamp, fields disabled.
3. The API refuses the same way. This is the request the official checker sends to the
   closed fixture event:

   ```bash
   curl -X POST -H "Authorization: Bearer bh_demo_participant_2026" \
        -H "Content-Type: application/json" -d '{"title":"late"}' \
        http://localhost:8080/api/v1/events/evt_01/projects
   # {"error":{"code":"submissions_closed","message":"Submissions for Sample Hack 2026 closed at 2026-03-01 18:00 UTC. ..."}}
   ```

## 2:30 Assign and score (Organizer → Judge A)

1. Organizer: *Assignments* → *Preview* (the plan, with any shortfalls) → *Assign*.
2. Judge A: *My events → Demo Day* → **Owl Lamp** → the scorecard. Mark the cells; the
   weighted total updates as you go. *Save*.
3. Organizer: *Live progress* shows the review done (it refreshes every five seconds).

## 3:15 Publish (Organizer)

1. *Results* tab: the ranking, the 95% interval, the method comparison.
2. *Publish using this method*. The results are frozen, judging closes, the prize winner
   is filled in, and the results are signed.
3. Open the event page as a visitor: results and the prize winner are public. Click
   *signed record*: VERIFIED, with the JSON and the key id.

## 3:45 Why you can trust the judging (the fixture event)

1. Organizer: *My events → Sample Hack 2026 → Live progress*. `jdg_07` is flagged
   **flat** (the same score for every project); `jdg_01` and `jdg_23` are flagged
   **single** (one review, so no bias estimate).
2. *Results*: switch the method between judge-effect, z-score, raw and Bradley–Terry.
   On this data the judges do not differ beyond noise, so the default barely moves from
   the raw mean (0.998), while z-scores reshuffle the top five correcting biases that
   are not there. The yellow notice: places 1 to 4 are statistical ties.
3. Isolation, from a terminal:

   ```bash
   curl -H "Authorization: Bearer bh_demo_judge_b_2026" http://localhost:8080/api/v1/judges/jdg_24/scores
   # 403 {"error":{"code":"permission_denied","message":"Judges can only read their own scores."}}
   ```

4. *Audit trail*: "Chain intact: N entries verified", then the plain-language history:
   who scored what, the deadline change from a minute ago, the publication.

## 4:30 Community and extras (optional, if time allows)

- **Open Jam 2026** as Participant: *Vote for your favourites*. The list order differs
  per voter, and the totals say "hidden until voting closes", for organizers too.
- `/embed/sample-hack-2026/`: the embeddable gallery widget.
- `/api/docs/`: the REST API reference, served offline.

## 5:00 Close

```bash
python3 run.py .dogfood.toml
# claimed T1 T2 T3 T4, verified T1 T2   (run.py has no T3/T4 checks)
python3 tools/check_extended.py .dogfood.toml
# verified T3 T4 (of T3 T4)
```
