"""The seeded demo accounts: who they are, and the fixed credentials the checker uses.

Everything here is inert unless DEMO_MODE is on: the seed does not create these
credentials, the one-click switcher returns 404, bearer authentication refuses any
token starting with TOKEN_PREFIX, and `manage.py disarm_demo` (run by the container on
every boot outside demo mode) revokes the tokens and scrubs the known passwords from a
database that was once a demo.
"""

from django.urls import reverse

ADMIN_EMAIL = "admin@bananahat.localhost"
ORGANIZER_EMAIL = "organizer@bananahat.localhost"

# The fixture judges who play judge A and judge B in .dogfood.toml. jdg_24 and jdg_26
# wrote the most reviews, so their score lists are the most interesting to look at.
JUDGE_A_ID = "jdg_24"
JUDGE_B_ID = "jdg_26"
# Captain of tm_01, whose project is Glass Signal, the first fixture project.
PARTICIPANT_TEAM_ID = "tm_01"

TOKEN_PREFIX = "bh_demo_"  # noqa: S105 (a prefix, published on purpose)
TOKENS = {
    "organizer": "bh_demo_organizer_2026",
    "judge_a": "bh_demo_judge_a_2026",
    "judge_b": "bh_demo_judge_b_2026",
    "participant": "bh_demo_participant_2026",
    "admin": "bh_demo_admin_2026",
}
PASSWORDS = {
    "organizer": "organizer-demo-2026",
    "judge_a": "judge-a-demo-2026",
    "judge_b": "judge-b-demo-2026",
    "participant": "participant-demo-2026",
    "admin": "admin-demo-2026",
}


def accounts():
    """[{key, label, email}] for the login page, resolved against the database."""
    from accounts.models import User
    from events.models import EventRole, Team

    rows = [("admin", "Platform admin", ADMIN_EMAIL), ("organizer", "Organizer", ORGANIZER_EMAIL)]
    for key, label, public_id in (("judge_a", "Judge A", JUDGE_A_ID), ("judge_b", "Judge B", JUDGE_B_ID)):
        role = EventRole.objects.select_related("user").filter(public_id=public_id).first()
        if role:
            rows.append((key, f"{label} · {public_id}", role.user.email))
    team = Team.objects.filter(public_id=PARTICIPANT_TEAM_ID).first()
    if team:
        captain = team.memberships.select_related("user").order_by("-is_captain", "joined_at").first()
        if captain:
            rows.append(("participant", f"Participant · {team.name}", captain.user.email))
    names = dict(User.objects.filter(email__in=[r[2] for r in rows]).values_list("email", "name"))
    return [
        {"key": key, "label": label, "email": email, "name": names.get(email, email)}
        for key, label, email in rows
        if email in names
    ]


def landing_url(email):
    for account in accounts():
        if account["email"] == email:
            if account["key"] in {"judge_a", "judge_b"}:
                return reverse("judging:home")
            if account["key"] == "organizer":
                return reverse("events:mine")
    return reverse("home")
