"""The official DOGFOOD checker, run.py, against a live server in the test run.

This is the same program the organisers run, unmodified, pointed at a real HTTP server
started by the test. If it does not print "verified T1 T2", the build fails.
"""

import subprocess
import sys

import pytest

from tests.conftest import FIXTURES, REPO

TOML = """
[portal]
base_url = "{url}"

[tiers]
claimed = ["T1", "T2"]
pitch = "test"

[auth]
organizer   = "Authorization: Bearer bh_demo_organizer_2026"
judge_a     = "Authorization: Bearer bh_demo_judge_a_2026"
judge_b     = "Authorization: Bearer bh_demo_judge_b_2026"
participant = "Authorization: Bearer bh_demo_participant_2026"

[routes]
gallery      = "/projects/"
submit       = "/api/v1/events/evt_01/projects"
judge_scores = "/api/v1/judges/me/scores"
peer_scores  = "/api/v1/judges/jdg_24/scores"
csv_export   = "/api/v1/events/evt_01/exports/scores.csv"
"""


def test_committed_config_uses_the_same_routes():
    committed = (REPO / ".dogfood.toml").read_text(encoding="utf-8")
    for line in TOML.strip().splitlines():
        if "=" in line and "base_url" not in line and "pitch" not in line and "claimed" not in line:
            key = line.split("=")[0].strip()
            value = line.split("=", 1)[1].strip()
            assert value in committed, f"{key} differs between the test and .dogfood.toml"


@pytest.mark.django_db(transaction=True)
def test_run_py_verifies_t1_and_t2(live_server, tmp_path, settings):
    settings.ALLOWED_HOSTS = ["*"]
    config = tmp_path / ".dogfood.toml"
    config.write_text(TOML.format(url=live_server.url), encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "run.py"), str(config), "--fixtures", str(FIXTURES)],
        capture_output=True, text=True, timeout=120,
    )
    print(result.stdout)
    assert "FAIL" not in result.stdout
    assert "claimed T1 T2, verified T1 T2" in result.stdout
