# Banana Hat

A self-hostable hackathon submission and judging platform, being built for
[DOGFOOD 2026](https://dogfoodhack.com). Apache-2.0.

```
docker compose up
```

Then open http://localhost:8080. The web container loads `fixtures.json` and prints demo
bearer tokens for the organizer, two judges and a participant; the sign-in page has a
one-click demo account switcher. Mail is caught by Mailpit at http://localhost:8025.

Check it with the official acceptance checker:

```
python3 run.py .dogfood.toml
```

Development: `pip install -r requirements-dev.txt`, then `pytest`.

Documentation is on its way: architecture, data model, judging math, threat model.
