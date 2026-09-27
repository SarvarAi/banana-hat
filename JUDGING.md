# Judging

How projects are assigned to judges, how scores become a ranking, how judges are
evened out, and why each choice is the one we made. Every number in this document is
reproduced by a single command against the seeded portal:

```bash
docker compose exec web python src/manage.py normalization_report --runs 1000
```

The math lives in [`src/judging/stats.py`](src/judging/stats.py): about 400 lines of plain
Python with no Django and no numpy, so anyone can read it, test it, and re-run it by
hand. [`tests/test_stats.py`](tests/test_stats.py) checks each estimator on data where
the right answer is known.

- [1. Assignment](#1-assignment)
- [2. Scoring and weights](#2-scoring-and-weights)
- [3. Normalization: the judge-effect model](#3-normalization-the-judge-effect-model)
- [4. The alternatives we ship, and why they are not the default](#4-the-alternatives-we-ship-and-why-they-are-not-the-default)
- [5. Proof on the fixture data](#5-proof-on-the-fixture-data)
- [6. Uncertainty and ties](#6-uncertainty-and-ties)
- [7. The fixture's awkward cases](#7-the-fixtures-awkward-cases)
- [8. Pairwise mode (Bradley–Terry)](#8-pairwise-mode-bradleyterry)
- [9. Isolation: what a judge can and cannot see](#9-isolation-what-a-judge-can-and-cannot-see)
- [10. Publishing](#10-publishing)
- [11. Limitations](#11-limitations)

---

## 1. Assignment

Code: [`src/judging/assignment.py`](src/judging/assignment.py). The organizer sets a
target number of reviews per project (default 3) and optionally a cap per judge, then
presses **Preview** and **Assign** in the console, or calls
`POST /api/v1/events/{id}/assignments/auto`.

1. **Eligibility.** A judge may review a project if the judge covers its track (a judge
   with no tracks covers all of them), has not declared a conflict of interest with its
   team, is not on the team, is not already assigned, and has not recused from it before.
2. **Most constrained first.** Projects are ordered by how many judges are eligible for
   them. A track with three judges is served before a track with eight, so the thin
   track is not starved by projects that had plenty of options.
3. **Round robin.** Each round gives every under-covered project one more reviewer.
   Filling one project completely before moving on would put the whole shortfall on
   whichever projects came last.
4. **Least-loaded judge wins**, and among equally loaded judges, **the one who has
   co-reviewed least with the project's existing reviewers**. The second rule exists for
   the statistics in section 3: a judge's bias can only be estimated relative to the
   judges they share projects with. Varied pairings keep the judge graph connected and
   the bias estimates well conditioned. Two judges who always review the same three
   projects together are, statistically, one judge.
5. **Deterministic.** Remaining ties are broken by a seeded RNG, and the seed is written
   to the audit log with the plan, so the same inputs always give the same assignment
   and any plan can be replayed.
6. **Honest shortfalls.** When a project cannot reach the target (not enough judges in its
   track, or everyone left is at their cap or conflicted), the plan says so, project by
   project, instead of silently assigning fewer.

On the fixture event, running the assignment after import tops up the 8 projects that
had only two reviews, creating 8 pending reviews. The dashboard shows them as the
outstanding work.

**Conflicts of interest.** A judge who recuses from a project (from the scoring page, or
`POST /api/v1/assignments/{id}/recuse`) gives a reason, their score for it (if any) is
voided, a `Conflict` row is written so they are never assigned to that team again, and
the project shows up as under-covered on the dashboard. Recusal voids a score, so it
obeys the same window as scoring: once scores lock or results are published, it is
refused. Judges cannot join a team in an
event they judge, and a judge cannot also be an organizer of that event. The second rule
exists because organizers can read every score, which would make isolation a fiction.

## 2. Scoring and weights

A judge scores every criterion of the rubric on the event's scale (default 1 to 5) and
may leave a comment only organizers see. A scorecard with a criterion missing is refused;
half a review is not a review.

Weights are relative (2, 1, 1 means the first counts for half), and they are applied
**when results are computed, never when a judge scores**. For a review `r` with values
`v_rc` and weights `w_c`:

```
y_r = Σ_c w_c · v_rc  /  Σ_c w_c
```

So an organizer can reweight the rubric after judging has started and nobody re-scores.
The ranking recomputes, the change goes in the audit trail with before and after, and
the scorecards stay a faithful record of what each judge actually said. Adding or
removing criteria is refused once anyone has scored, because existing scorecards would
no longer match.

The seeded fixture event uses functionality 40, quality 30, innovation 30. The fixture
says nothing about weights; an organizer would, so the demo does.

## 3. Normalization: the judge-effect model

Some judges are generous and some are harsh. If a generous judge happens to review your
project and a harsh one reviews your neighbour's, the raw average ranks you higher for
no reason of your own. Normalization tries to remove that.

### The model

```
y_r  =  θ_p  +  b_j  +  ε_r
```

- `θ_p` is the quality of project `p`: what an average judge would give it.
- `b_j` is the leniency of judge `j`: positive for generous judges, negative for harsh ones.
- `ε_r` is everything else about this particular review.

We estimate `θ` and `b` together by ridge-regularised least squares:

```
minimise   Σ_r ( y_r − θ_p(r) − b_j(r) )²   +   λ · Σ_j b_j²
```

This is the maximum a posteriori estimate of a standard mixed model with fixed project
effects and random judge effects, `b_j ~ N(0, τ²)` and `ε ~ N(0, σ²)`, with `λ = σ²/τ²`.
Read `λ` as a prior measured in reviews: a judge's bias is shrunk toward zero as if they
had also written `λ` perfectly unbiased reviews. A judge with one review barely moves
from zero. A judge with eleven consistent reviews moves most of the way to what their
data says.

### Why this and not z-scores: the model knows who saw what

The textbook normalization is per-judge z-scoring: subtract each judge's mean, divide by
their standard deviation. That assumes every judge saw a random sample of all projects.
Hackathons assign by track, so they do not: a judge in a strong track has a high mean
because their projects are good. Z-scoring calls that leniency and subtracts it, and
the best track's projects are pulled down.

The judge-effect model estimates `b_j` only from the comparisons the data supports: how
judge `j` scored the same projects that other judges also scored. A judge who drew a
strong batch, and scored it the way other judges scored those same projects, gets
`b_j ≈ 0`. Section 5 measures the difference.

### Solving it exactly

Setting the gradient to zero gives, for every project, `θ_p = ȳ_p − b̄_p` (the project's
mean score minus the mean bias of its reviewers). Substituting back leaves one linear
equation per judge:

```
(n_j + λ) · b_j  −  Σ_{r ∈ j}  b̄_p(r)   =   Σ_{r ∈ j} ( y_r − ȳ_p(r) )
```

That is a `J × J` system (30 × 30 for the fixture), solved exactly by Gaussian
elimination in `stats._solve`. The matrix is `λI + S`, where `S` is a sum over projects
of centring matrices and therefore positive semi-definite, so for any `λ > 0` it is
positive definite and the solution is unique. That holds even when some judges share no
projects at all: the ridge term pins down what the data cannot.

### Choosing λ from the data

`λ` is not a tuning knob. It is estimated from the scores by empirical Bayes: an EM-style
iteration alternates between fitting the model and re-estimating the two variances,

```
σ² = RSS / (N − P − Σ_j n_j/(n_j + λ))        residual variance, corrected for fitted df
τ² = ( Σ_j b_j²  +  Σ_j σ²/(n_j + λ) ) / J      variance of the judge effects
λ  = σ² / τ²
```

It is clamped to `[0.5, 50]`. The lower bound stops a handful of reviews from producing
wild corrections. The upper bound corresponds to "judges effectively do not differ".
The key property: **when judges genuinely do not differ, λ grows, the biases vanish, and
the model returns the raw means.** It declines to invent corrections the data does not
support. That is exactly what happens on the fixture (section 5).

### A useful property: weight first or normalize first, same answer

For a fixed `λ`, the estimator is linear in `y` with a design that does not depend on
which criterion is being normalized. So normalizing each criterion separately and then
weighting gives exactly the same `θ` as weighting first and normalizing the total. We
normalize the weighted total and lose nothing. (Z-scoring does not have this property,
because its standard deviations depend on the data.)

## 4. The alternatives we ship, and why they are not the default

The organizer can pick any of these per event, and the Results tab shows them side by
side with each one's rank correlation against the raw mean.

| Method | What it does | Why it is not the default |
|---|---|---|
| `raw` | Mean weighted score. | Ignores judge leniency entirely. Worst under bias (section 5). |
| `zscore` | Per-judge standardisation, shrunk toward the pool with a prior of 3 reviews so single-review and flat judges do not divide by zero. | Assumes each judge saw a random sample of projects. Loses when tracks differ in quality, and when there is no bias at all. |
| `judge_effect` | The model in section 3. **Default.** | |
| `implied_bt` | Reads each judge's scores as a ranking of the projects they saw and fits Bradley–Terry to the implied pairwise wins, weighting each judge's pairs by 1/(n−1). A judge's level cannot matter by construction. | Throws away how much better one project was than another, so it is noisier. Useful as a robustness check, and a flat judge contributes nothing at all (only ties). |
| `pairwise` | Bradley–Terry on explicit head-to-head judgements (section 8). | A different judging mode, not a normalization of rubric scores. |

## 5. Proof on the fixture data

### What the fixture's scores say

After excluding the duplicate `prj_41`: **122 counted reviews of 40 projects by 30
judges**. Eight projects have 2 reviews, 26 have 3, two have 4, four have 5. Judges wrote
between 1 and 11 reviews. `jdg_01` and `jdg_23` wrote one each. `jdg_07` gave every
project the same total. The judge graph is **connected** (one component), so every
judge's leniency is comparable with every other's.

The judge-effect model estimates **λ = 50 (the upper bound), judge-bias σ_b = 0.093,
residual σ = 0.661**. In words: the differences between these judges' averages are no
larger than you would expect from noise, given how few reviews most of them wrote. With
`λ` forced down to 3 for comparison, the most extreme apparent biases are `jdg_10 −0.35
(3 reviews)`, `jdg_01 −0.34 (1 review)`, `jdg_02 +0.35 (6)`, `jdg_15 +0.28 (6)`: small
samples, which is exactly what empirical Bayes should decline to act on. So on this
data the default method's ranking matches the raw mean almost exactly:

| method | top 5 | Spearman vs raw |
|---|---|---|
| raw | prj_11, prj_34, prj_10, prj_25, prj_37 | 1.000 |
| zscore | prj_34, prj_11, prj_33, prj_10, prj_37 | 0.971 |
| **judge_effect** | prj_11, prj_34, prj_10, prj_25, prj_37 | **0.998** |
| implied_bt | prj_34, prj_33, prj_16, prj_11, prj_37 | 0.788 |

Z-scoring reshuffles the top five on this data because it corrects biases that are not
there. That is the "invented correction" failure, and it is visible.

### Does it work when judges *are* biased? A simulation on the fixture's own graph

The fixture cannot tell us which method is right, because nobody knows the true quality
of its projects. So we simulate events where we do know it, using **the fixture's exact
judge-project assignment graph**: the same 30 judges, the same 122 pairings, the same
unfinished batches. In each run:

- every project gets a true quality `q_p ~ 3.3 + N(0, 0.6²)`, plus a per-track offset in
  the scenario where tracks differ;
- every judge gets a bias `b_j ~ N(0, s²)`;
- each of the three criteria is scored `round(q_p + b_j + N(0, 0.6²))`, clipped to 1..5,
  like a real scorecard, and the review's total is their mean;
- in the flat-judge scenario, two judges give every project the same score.

Each method then ranks the projects, and we measure the Spearman correlation between
its ranking and the true one (1.0 is perfect). In brackets is the share of the true top
five that the method put in its top five. 1000 runs per scenario, seed 2026:

| scenario | raw | zscore | judge_effect | implied_bt |
|---|---|---|---|---|
| No judge bias at all | **0.920 (0.74)** | 0.899 (0.71) | 0.918 (0.74) | 0.833 (0.60) |
| Moderate judge bias (sd 0.5) | 0.836 (0.63) | **0.875 (0.67)** | 0.875 (0.67) | 0.832 (0.60) |
| Strong judge bias (sd 0.8) | 0.744 (0.54) | **0.843 (0.63)** | 0.842 (0.63) | 0.831 (0.59) |
| Bias, and tracks that genuinely differ (sd 0.5) | 0.884 (0.68) | 0.872 (0.67) | **0.912 (0.73)** | 0.760 (0.54) |
| Strong bias plus two flat judges | 0.711 (0.52) | **0.818 (0.60)** | 0.816 (0.61) | 0.809 (0.55) |
| **Worst-case shortfall vs the best method** | 0.107 | 0.041 | **0.002** | 0.152 |

### Reading the table, and the argument for the default

No method wins every row, and we do not pretend otherwise:

- With **no bias**, raw means are best (there is nothing to correct), and z-scoring pays
  0.021 for correcting noise.
- With **bias only**, z-scoring edges ahead by 0.001–0.002. It also corrects each judge's
  *spread*, which clipping at 1 and 5 distorts. Our model corrects only level.
- With **bias and tracks of different strength**, the realistic hackathon, the
  judge-effect model wins clearly (0.912 against 0.872 for z-scores). Z-scoring falls
  *below the raw mean* here, for the reason given in section 3.

An organizer does not know in advance which of these events they are running. So the
right criterion is **worst-case regret**: in the scenario that is worst for it, how far
behind the best method does each one fall? The judge-effect model's worst case is
**0.002**. Z-scoring's is 0.041, the raw mean's is 0.107. That is why it is the default.

`tests/test_stats.py` pins the two claims that matter as tests, on the real fixture
design: the default beats raw means under bias, and beats z-scoring when tracks differ.

## 6. Uncertainty and ties

Every normalized score comes with an approximate standard error, `σ̂ / √n_p`, and a 95%
interval, reported inside the scale ("up to 5.02" is shown as 5). When the intervals of
two adjacent places among the top four overlap, the Results tab says so in words:
"places 1 and 2 are within each other's 95% interval: the evidence does not really
separate them". On the fixture, the top four are all statistical ties. That is the truth
about 2 to 5 reviews per project, and an organizer deciding an 800 USD prize should see
it before publishing. They can then assign more reviews to those projects.

The interval ignores the uncertainty in the judge biases, so it is slightly too narrow.
It is a guide to "is this gap real", not a formal test.

## 7. The fixture's awkward cases

| Case | What happens |
|---|---|
| **A judge who gave everything the same score** (`jdg_07`, 4/4/4 on three projects) | Flagged `flat` on the live dashboard. In the judge-effect model their constant level is absorbed into `b_j`, so they do not lift or sink their projects as a group; they add noise, not bias. In `implied_bt` they contribute only ties, i.e. nothing. The organizer can void their scores from the Assignments tab, with a reason that goes in the audit trail. |
| **Judges with one review** (`jdg_01`, `jdg_23`) | Flagged `single`: their bias cannot be estimated. Shrinkage keeps it near zero, so their one score counts at face value rather than being "corrected" on no evidence. |
| **Unfinished batches** (8 projects with 2 reviews) | They still rank, with wider intervals. The seed runs automatic assignment, which tops them up with 8 pending reviews, and the dashboard lists them under "Needs attention". |
| **The duplicate submission** (`prj_41`, same team, title and repository as `prj_07`, submitted 13 hours later) | Kept, never deleted, flagged `duplicate_of = prj_07`, hidden from the gallery, the ballot and the rankings. Its 4 reviews are excluded from estimation too: three of its judges also reviewed `prj_07`, and counting the same repository twice would double-weight their opinion. The organizer can swap which copy competes, or confirm the flag, from the Submissions tab. |
| **Three teams called StillTrail** (and two more name clashes) | Not an error: names are labels. The import warns, and the teams are told apart by id. |

## 8. Pairwise mode (Bradley–Terry)

Setting an event's method to `pairwise` gives judges a **Compare** page: two projects
from their tracks, "which is better?". No scale to calibrate, and judge leniency cannot
exist by construction.

The estimator is Bradley–Terry, `P(i beats j) = s_i / (s_i + s_j)`, fitted by the
minorisation–maximisation algorithm of Hunter (2004):

```
s_i  ←  (W_i + a)  /  ( Σ_j n_ij / (s_i + s_j)  +  2a / (s_i + 1) )
```

where `W_i` is `i`'s wins and `n_ij` the comparisons between `i` and `j`. The `a` terms
are a weak prior: every project also plays `a = 1` virtual win and one virtual loss
against a fixed reference project of strength 1. That keeps a project that won (or lost)
every comparison at a finite strength, puts projects nobody has compared at exactly
the reference, and anchors the scale so no renormalisation is needed. Scores are
reported as log-strengths: 0 is an average project, +1 means e times the odds of beating
one. A test checks that 300 wins to 100 recovers odds of 3 : 1 within 2%.

**Pair selection.** Among pairs this judge has not yet compared, choose the pair compared
least overall, and among those the two projects whose current strengths are closest. A
comparison between a clear favourite and a clear outsider teaches the model almost
nothing. Left and right are randomised to avoid position bias.

## 9. Isolation: what a judge can and cannot see

| | Judge | Organizer | Participant | Visitor |
|---|---|---|---|---|
| Their own scores | read, write until judging closes | read | – | – |
| Another judge's scores | **403** | read | 403 | 401 |
| Rankings before publication | **403** | read, preview | 403 | 403 |
| Write any judge's score | only their own | **never** (void only, with a reason) | – | – |
| Audit trail | 403 | read | 403 | 401 |

How it is enforced, in layers ([ARCHITECTURE.md](ARCHITECTURE.md) has the details):

1. **`Score.objects.visible_to(user)`** is the only way views fetch scores. It returns a
   judge's own rows, all rows of events the user organizes, or nothing. A view that
   forgot to filter still could not leak, because the rows were never fetched.
2. **Explicit refusals.** `GET /api/v1/judges/jdg_24/scores` as another judge is a 403,
   not an empty list, and so is `?judge=jdg_24` on the "my scores" endpoint. An unknown
   judge id gets the same 403, so ids cannot be probed.
3. **Default deny.** Every API view must declare its permission classes; the project
   default is `DenyAll`, and a test walks every URL and fails the build if any view
   relies on a default. Every HTML view must declare an access policy; middleware
   refuses any that does not.
4. **Role separation in the data.** A judge cannot be a participant or an organizer of
   the same event (`events.services.grant_role`).
5. **Tests that behave like curl.** `tests/test_access_matrix.py` sends every sensitive
   request as every role and checks the status codes. The official `run.py` runs in the
   test suite against a live server.

## 10. Publishing

Publishing (Results tab, or `POST /api/v1/events/{id}/publish`) is refused while
submissions or community voting are still open. When it happens:

- the ranking under the chosen method is frozen into a `ResultSnapshot`, and the public
  only ever sees that snapshot;
- judging closes, so no score can move under a published ranking;
- prize winners are filled in from the ranking (track prizes from the track's ranking,
  and no project wins two prizes);
- the results are **signed** with the portal's Ed25519 key, including the audit chain's
  head digest at that moment, so the published ranking and the history behind it are
  bound together outside the database;
- every judge with at least one review gets a **signed participation record**
  containing a salted SHA-256 commitment to their scores. It reveals nothing, but if an
  audit ever needs it, revealing the salt proves exactly what they submitted.

If a mistake is found, **Unpublish** takes the results down with a mandatory reason
(audited) and reopens judging. The old snapshot is kept, and every signed record of the
event (results, certificates, judge records) is revoked, because each describes results
or scores that may now change. The next publication issues fresh ones, so a judge's
score commitment always covers the scores behind the results currently published.

## 11. Limitations

- **Additive bias only.** The model corrects a judge's level, not their spread. A judge
  who uses only 3 and 4 while others use 1 to 5 is not stretched. The simulation above
  bounds what this costs (0.002 at worst on the fixture graph), and z-scores are one
  click away if an organizer disagrees.
- **Scores are ordinal, treated as interval.** "4 minus 3" is assumed to mean the same as
  "3 minus 2". Every rubric-averaging platform makes this assumption; `implied_bt` does
  not, if it matters to you.
- **Small samples are small.** With 2 to 5 reviews per project, no method can separate
  close projects reliably. We report the uncertainty instead of hiding it.
- **The λ bounds are a judgement call.** [0.5, 50] was chosen so that a bias is never
  corrected on less than half a review's worth of evidence, and "no difference between
  judges" is representable.
- **Intervals are approximate** (section 6).
- **Pairwise mode needs volume.** A reliable pairwise ranking of 40 projects needs a few
  hundred comparisons. The pair selector is informative, but it is not a formal
  active-learning design.
