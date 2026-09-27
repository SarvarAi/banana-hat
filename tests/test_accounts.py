"""Sign-up, sign-in, tokens and account claiming."""

import re

from accounts.models import ApiToken, User


def test_signup_creates_account_and_signs_in(web):
    client = web()
    response = client.post("/accounts/signup/", {"name": "New Person", "email": "New@Example.net",
                                                 "password": "a-long-enough-passphrase"})
    assert response.status_code == 302
    user = User.objects.get(email="new@example.net")
    assert client.get("/accounts/").status_code == 200
    assert user.check_password("a-long-enough-passphrase")


def test_signup_refuses_weak_password_and_duplicate_email(web, participant):
    client = web()
    weak = client.post("/accounts/signup/", {"name": "W", "email": "w@example.net", "password": "password"})
    assert weak.status_code == 200 and not User.objects.filter(email="w@example.net").exists()
    dup = client.post("/accounts/signup/", {"name": "D", "email": participant.email.upper(),
                                            "password": "another-long-passphrase"})
    assert b"already exists" in dup.content


def test_login_is_rate_limited(web):
    client = web()
    codes = [client.post("/accounts/login/", {"username": "nobody@example.net", "password": "wrong"}).status_code
             for _ in range(12)]
    assert codes[0] == 200 and codes[-1] == 429


def test_imported_user_claims_account_by_reset_link(web, mailoutbox):
    imported = User.objects.get(email="member1_1@example.org")
    assert not imported.has_usable_password()
    client = web()
    client.post("/accounts/password-reset/", {"email": "member1_1@example.org"})
    assert len(mailoutbox) == 1
    link = re.search(r"https?://[^\s]+/accounts/password-reset/[^\s]+/", mailoutbox[0].body).group(0)
    path = link.split("testserver", 1)[1]
    form = client.get(path, follow=True)
    assert form.status_code == 200
    done = client.post(form.redirect_chain[-1][0], {"new_password1": "claimed-account-pass",
                                                    "new_password2": "claimed-account-pass"})
    assert done.status_code == 302
    imported.refresh_from_db()
    assert imported.check_password("claimed-account-pass")


def test_reset_for_unknown_address_looks_the_same(web, mailoutbox):
    response = web().post("/accounts/password-reset/", {"email": "nobody@nowhere.example"})
    assert response.status_code == 302 and response["Location"].endswith("/sent/")
    assert len(mailoutbox) == 0


def test_tokens_are_shown_once_and_stored_hashed(web, judge_a):
    client = web(judge_a)
    client.post("/accounts/tokens/", {"token-name": "ci bot", "token-expires_in_days": "30"})
    page = client.get("/accounts/").content.decode()
    raw = re.search(r"bh_[A-Za-z0-9_\-]{20,}", page).group(0)
    token = ApiToken.objects.get(name="ci bot")
    assert token.digest == ApiToken.hash(raw) and raw not in token.digest
    assert raw not in client.get("/accounts/").content.decode()  # gone on the next load


def test_demo_login_only_in_demo_mode(web, settings, organizer):
    settings.DEMO_MODE = False
    assert web().post("/accounts/demo-login/", {"email": organizer.email}).status_code == 404
    settings.DEMO_MODE = True
    assert web().post("/accounts/demo-login/", {"email": organizer.email}).status_code == 302
    assert web().post("/accounts/demo-login/", {"email": "stranger@example.net"}).status_code == 404
