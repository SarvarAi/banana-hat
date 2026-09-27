#!/usr/bin/env python3
"""Banana Hat's own acceptance checks for T3 and T4, in the style of run.py.

The official checker, run.py, only has checks for T1 and T2, so it always reports T3
and T4 as "claimed but not verified". This script is how we back those claims: it
makes real HTTP requests to a running portal and prints what it verified.

    python3 tools/check_extended.py .dogfood.toml > acceptance-report-extended.txt

Standard library only, like run.py. It reads base_url and the auth headers from
.dogfood.toml and knows Banana Hat's own routes. It changes demo data a little: it
posts one comment, creates and deletes a webhook, issues a signed judge record and
imports one unlisted copy of the fixture event. The rate-limit check runs last,
because it deliberately exhausts the anonymous request budget for a minute.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_record import canonical, ed25519_verify, unb64u  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

TIMEOUT = 15
FIXTURE_EVENT = "evt_01"
OPEN_EVENT_SLUG = "open-jam"
JUDGE_A = "jdg_24"


def load_config(path):
    if tomllib:
        with open(path, "rb") as f:
            return tomllib.load(f)
    data, section = {}, None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#")[0].strip()
            head = re.fullmatch(r"\[([A-Za-z0-9_.]+)\]", line)
            if head:
                section = data.setdefault(head.group(1), {})
            elif "=" in line and section is not None:
                key, _, value = line.partition("=")
                section[key.strip()] = value.strip().strip('"')
    return data


class Portal:
    def __init__(self, base, auth):
        self.base, self.auth = base.rstrip("/"), auth

    def request(self, path, role=None, method="GET", body=None):
        req = urllib.request.Request(self.base + path, method=method)  # noqa: S310 (http(s) from the config)
        if role:
            name, _, value = self.auth[role].partition(":")
            req.add_header(name.strip(), value.strip())
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, {}, f"{type(e).__name__}: {e}"

    def json(self, *args, **kwargs):
        status, headers, text = self.request(*args, **kwargs)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, None


class Check:
    def __init__(self, tier, label):
        self.tier, self.label, self.ok, self.detail = tier, label, False, []

    def expect(self, condition, note):
        if not condition:
            self.detail.append(note)
        return condition


def run(portal):
    checks = []

    def check(tier, label):
        c = Check(tier, label)
        checks.append(c)
        return c

    status, events = portal.json("/api/v1/events")
    open_event = next((e for e in (events or {}).get("results", []) if e["slug"] == OPEN_EVENT_SLUG), None)
    open_id = open_event["id"] if open_event else "missing"

    # --- T3 -----------------------------------------------------------------------
    c = check("T3", "ballot order is random per voter, stable per ballot")
    s1, a1 = portal.json(f"/api/v1/events/{open_id}/ballot", "judge_a")
    s1b, a1b = portal.json(f"/api/v1/events/{open_id}/ballot", "judge_a")
    s2, b1 = portal.json(f"/api/v1/events/{open_id}/ballot", "judge_b")
    if c.expect(s1 == s1b == s2 == 200, f"GET ballot as two judges: got {s1}, {s1b}, {s2}"):
        order_a = [p["id"] for p in a1["projects"]]
        c.expect(order_a == [p["id"] for p in a1b["projects"]], "the same ballot came back in a different order")
        c.expect(order_a != [p["id"] for p in b1["projects"]] or len(order_a) < 3,
                 "two voters got the same order")
    c.ok = not c.detail

    c = check("T3", "vote totals hidden while voting is open")
    for role in (None, "organizer"):
        status, body = portal.json(f"/api/v1/events/{open_id}/votes", role)
        c.expect(status == 403 and (body or {}).get("error", {}).get("code") == "results_hidden",
                 f"GET votes as {role or 'anonymous'}: got {status}, wanted 403 results_hidden")
    c.ok = not c.detail

    c = check("T3", "participant can comment, visitor cannot")
    status, projects = portal.json(f"/api/v1/events/{open_id}/projects")
    target = next((p["id"] for p in (projects or []) if p["team"]["name"] != "NorthKiln"), None)
    s_anon, _ = portal.json(f"/api/v1/projects/{target}/comments", None, "POST", {"body": "hi"})
    # Unique text: posting the same comment twice is refused as a duplicate, by design.
    s_part, _ = portal.json(f"/api/v1/projects/{target}/comments", "participant", "POST",
                            {"body": f"Checked by tools/check_extended.py at {time.time():.0f}"})
    c.expect(s_anon in (401, 403), f"anonymous comment: got {s_anon}, wanted 401/403")
    c.expect(s_part == 201, f"participant comment: got {s_part}, wanted 201")
    c.ok = not c.detail

    c = check("T3", "duplicate submission flagged and kept out")
    status, dup = portal.json("/api/v1/projects/prj_41", "organizer")
    c.expect(status == 200 and (dup or {}).get("duplicate_of") == "prj_07",
             f"GET prj_41 as organizer: got {status}, duplicate_of={(dup or {}).get('duplicate_of')}")
    status, gallery = portal.json(f"/api/v1/projects?event={FIXTURE_EVENT}&limit=100")
    ids = [p["id"] for p in (gallery or {}).get("results", [])]
    c.expect("prj_41" not in ids and "prj_07" in ids, "the public gallery still lists the duplicate")
    c.ok = not c.detail

    c = check("T3", "audit trail readable and intact")
    status, audit = portal.json(f"/api/v1/events/{FIXTURE_EVENT}/audit", "organizer")
    c.expect(status == 200 and audit and audit["entries"], f"GET audit as organizer: got {status}")
    c.expect(bool(audit and audit["chain"]["ok"]), "the hash chain did not verify")
    s_judge, _ = portal.json(f"/api/v1/events/{FIXTURE_EVENT}/audit", "judge_a")
    c.expect(s_judge == 403, f"GET audit as a judge: got {s_judge}, wanted 403")
    c.ok = not c.detail

    # --- T4 -----------------------------------------------------------------------
    c = check("T4", "REST API with an OpenAPI document")
    status, _, text = portal.request("/api/schema/?format=json")
    c.expect(status == 200 and '"openapi"' in text and "/api/v1/judges/{judge_id}/scores" in text,
             f"GET /api/schema/: got {status}")
    c.ok = not c.detail

    c = check("T4", "webhooks: create, test delivery, delete")
    status, hook = portal.json(f"/api/v1/events/{FIXTURE_EVENT}/webhooks", "organizer", "POST",
                               {"url": "https://example.invalid/hook", "event_types": ["results.published"]})
    if c.expect(status == 201 and hook and hook["secret"].startswith("whsec_"), f"POST webhook: got {status}"):
        s_ping, delivery = portal.json(f"/api/v1/webhooks/{hook['id']}/deliveries", "organizer", "POST")
        c.expect(s_ping == 200 and delivery and delivery["attempts"] == 1,
                 f"test delivery: got {s_ping}")
        s_judge, _ = portal.json(f"/api/v1/events/{FIXTURE_EVENT}/webhooks", "judge_a")
        c.expect(s_judge == 403, f"list webhooks as a judge: got {s_judge}, wanted 403")
        s_del, _ = portal.json(f"/api/v1/webhooks/{hook['id']}", "organizer", "DELETE")
        c.expect(s_del == 204, f"DELETE webhook: got {s_del}")
    c.ok = not c.detail

    c = check("T4", "signed judge record verifies offline")
    s_rec, record = portal.json(f"/api/v1/judges/{JUDGE_A}/record", "organizer", "POST")
    status, keys = portal.json("/.well-known/bananahat-keys.json")
    if c.expect(status == 200 and s_rec == 201, f"keys {status}, issue record {s_rec}"):
        key = {k["kid"]: k for k in keys["keys"]}.get(record["payload"]["key_id"])
        c.expect(key is not None, "the record's key is not published")
        if key:
            valid = ed25519_verify(unb64u(key["x"]), canonical(record["payload"]), unb64u(record["signature"]))
            c.expect(valid, "the signature does not verify against the published key")
            forged = dict(record["payload"], reviews=999)
            c.expect(not ed25519_verify(unb64u(key["x"]), canonical(forged), unb64u(record["signature"])),
                     "a tampered record still verified")
        s_other, _ = portal.json(f"/api/v1/judges/{JUDGE_A}/record", "judge_b")
        c.expect(s_other == 403, f"judge_b reading judge_a's record: got {s_other}, wanted 403")
    c.ok = not c.detail

    c = check("T4", "certificate page for a signed record")
    if record and record.get("record"):
        status, _, text = portal.request(f"/records/{record['record']}/certificate/")
        c.expect(status == 200 and "Certificate of judging" in text, f"GET certificate: got {status}")
    else:
        c.expect(False, "no record to render")
    c.ok = not c.detail

    c = check("T4", "embeddable gallery widget")
    status, headers, text = portal.request("/embed/sample-hack-2026/")
    csp = {k.lower(): v for k, v in headers.items()}.get("content-security-policy", "")
    xfo = {k.lower(): v for k, v in headers.items()}.get("x-frame-options")
    c.expect(status == 200 and "frame-ancestors *" in csp and xfo is None,
             f"GET /embed/: got {status}, frame-ancestors={'*' in csp}, x-frame-options={xfo}")
    s_js, _, js = portal.request("/static/js/embed.js")
    c.expect(s_js == 200 and "data-bananahat-gallery" in js, f"GET embed.js: got {s_js}")
    c.ok = not c.detail

    c = check("T4", "bulk export and re-import round trip")
    status, _, text = portal.request(f"/api/v1/events/{FIXTURE_EVENT}/exports/event.json", "organizer")
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if c.expect(status == 200 and doc and len(doc["projects"]) == 41, f"export event.json: got {status}"):
        doc["event"]["id"] = "evt_extended_check"
        doc["event"]["name"] = "Sample Hack 2026 (re-import check)"
        s_imp, report = portal.json("/api/v1/events/import", "organizer", "POST", doc)
        c.expect(s_imp == 201 and report["counts"]["projects"] == 41 and report["counts"]["scores"] == 126,
                 f"re-import: got {s_imp} {report and report.get('counts')}")
    c.ok = not c.detail

    # Last, because it spends the anonymous request budget for a minute.
    c = check("T3", "rate limiting refuses floods")
    statuses = [portal.request("/api/v1/projects?limit=1")[0] for _ in range(140)]
    c.expect(429 in statuses, f"140 anonymous requests in a burst: no 429 (saw {sorted(set(statuses))})")
    c.ok = not c.detail
    return checks


def main():
    ap = argparse.ArgumentParser(description="Banana Hat extended acceptance checks (T3, T4)")
    ap.add_argument("config", help="path to .dogfood.toml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    portal = Portal(cfg["portal"]["base_url"], cfg.get("auth", {}))
    print("Banana Hat extended acceptance report (T3, T4)")
    print(f"portal: {cfg['portal']['base_url']}")
    print("the official run.py has no T3/T4 checks; these are ours, same style")
    print()
    checks = sorted(run(portal), key=lambda c: c.tier)
    width = max(len(c.label) for c in checks) + 2
    for c in checks:
        print(f"{c.tier}  {c.label} {'.' * (width - len(c.label))} {'PASS' if c.ok else 'FAIL'}")
        for line in c.detail:
            print(f"       {line}")
    verified = [t for t in ("T3", "T4") if all(c.ok for c in checks if c.tier == t)]
    print()
    print(f"verified {' '.join(verified) or 'nothing'} (of T3 T4)")
    return 0 if len(verified) == 2 else 1


if __name__ == "__main__":
    sys.exit(main())
