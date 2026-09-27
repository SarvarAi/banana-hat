#!/usr/bin/env python3
"""Verify a Banana Hat signed record without trusting the portal that issued it.

    python3 tools/verify_record.py record.json --jwks keys.json
    python3 tools/verify_record.py record.json --jwks https://portal.example.org/.well-known/bananahat-keys.json

record.json is what /api/v1/records/<id> returns (or the JSON on a record's page).
keys.json is the portal's /.well-known/bananahat-keys.json, saved whenever you like:
the key that signed a record never changes, so a copy saved today verifies records
issued years from now by the same key.

Standard library only. Ed25519 verification is implemented below, straight from the
reference code in RFC 8032, section 6, so this file depends on nothing a sceptical
reader cannot read in five minutes. It is slow (tens of milliseconds) and not
constant-time, which is fine for checking a signature and wrong for making one.

Exit status: 0 valid, 1 invalid, 2 could not check.
"""

import argparse
import base64
import hashlib
import json
import sys
import urllib.request

# --- Ed25519 (RFC 8032, section 6) ------------------------------------------------

P = 2**255 - 19
Q = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _inv(x):
    return pow(x, P - 2, P)


def _add(p1, p2):
    a = (p1[1] - p1[0]) * (p2[1] - p2[0]) % P
    b = (p1[1] + p1[0]) * (p2[1] + p2[0]) % P
    c = 2 * p1[3] * p2[3] * D % P
    d = 2 * p1[2] * p2[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s, point):
    acc = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            acc = _add(acc, point)
        point = _add(point, point)
        s >>= 1
    return acc


def _equal(p1, p2):
    return (p1[0] * p2[2] - p2[0] * p1[2]) % P == 0 and (p1[1] * p2[2] - p2[1] * p1[2]) % P == 0


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


def _decompress(raw):
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


_GY = 4 * _inv(5) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decompress(public)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= Q:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % Q
    return _equal(_mul(s, G), _add(r, _mul(h, a)))


# --- records ----------------------------------------------------------------------

def unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(payload):
    """The exact bytes the portal signed: sorted keys, no whitespace, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load(source):
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=10) as response:  # noqa: S310
            return json.load(response)
    with open(source, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="Verify a Banana Hat signed record.")
    ap.add_argument("record", help="record JSON file (from /api/v1/records/<id>)")
    ap.add_argument("--jwks", help="the portal's keys: a saved file or the /.well-known URL")
    args = ap.parse_args()

    try:
        document = load(args.record)
        payload, signature = document["payload"], unb64u(document["signature"])
        key_id = payload["key_id"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot read the record: {exc}")
        return 2

    if args.jwks:
        try:
            keys = {k["kid"]: k for k in load(args.jwks)["keys"]}
        except (OSError, ValueError, KeyError) as exc:
            print(f"cannot read the keys: {exc}")
            return 2
        if key_id not in keys:
            print(f"INVALID: signed with key {key_id}, which that portal does not publish")
            return 1
        public = unb64u(keys[key_id]["x"])
        origin = f"the key {key_id} published by that portal"
    elif "public_key" in document:
        public = unb64u(document["public_key"])
        origin = ("the key embedded in the record (this proves the record is intact, "
                  "not who issued it; pass --jwks for that)")
    else:
        print("no key: pass --jwks")
        return 2

    if ed25519_verify(public, canonical(payload), signature):
        print(f"VALID: {payload.get('type')} {payload.get('record')} for {payload.get('event', {}).get('name')}")
        print(f"       issued {payload.get('issued_at')} by {payload.get('issuer')}, signed with {origin}")
        if payload.get("statement"):
            print(f"       \"{payload['statement']}\"")
        return 0
    print("INVALID: the signature does not match the contents")
    return 1


if __name__ == "__main__":
    sys.exit(main())
