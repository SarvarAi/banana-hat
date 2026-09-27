"""The committed OpenAPI document is the API: it must match the code exactly.

If this fails, regenerate it:
    cd src && python manage.py spectacular --file ../docs/openapi.yaml --validate
"""

from io import StringIO

from django.core.management import call_command

from tests.conftest import REPO


def test_committed_openapi_document_matches_the_code(tmp_path):
    generated = tmp_path / "openapi.yaml"
    call_command("spectacular", file=str(generated), validate=True, fail_on_warn=True, stderr=StringIO())
    def lines(path):  # the generator writes the platform's line endings
        return path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")

    assert lines(generated) == lines(REPO / "docs" / "openapi.yaml"), "docs/openapi.yaml is stale; regenerate it"


def test_every_api_route_is_documented():
    committed = (REPO / "docs" / "openapi.yaml").read_text(encoding="utf-8")
    for path in ["/api/v1/judges/{judge_id}/scores", "/api/v1/events/{event_id}/projects",
                 "/api/v1/events/{event_id}/votes", "/api/v1/records/{record_id}",
                 "/api/v1/events/{event_id}/judges/invitations", "/api/v1/events/{event_id}/audit"]:
        assert f"  {path}:" in committed, path
    assert "bearerAuth" in committed
