"""Public identifiers.

Rows have an integer primary key for joins and a public id for URLs and the API, in the
Stripe style: a type prefix and random characters, e.g. `prj_4k9w2m7q8x1d`. The prefix
makes a pasted id self-describing in a support thread, and the randomness means ids
leak neither row counts nor creation order.

Imported records keep their original id (the fixture's `prj_01`), which is what makes a
round trip through the interchange format lossless.
"""

import secrets

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, no i/l/o/u


def new_public_id(prefix: str, length: int = 12) -> str:
    return f"{prefix}_" + "".join(secrets.choice(_ALPHABET) for _ in range(length))
