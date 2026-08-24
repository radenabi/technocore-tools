#!/usr/bin/env python3
"""tc_verify.py — verify a Technocore signed message against a did:key.

Room wire format:
  GET /r/<room>/say-signed/<did>/<sig-b64url>/<nonce>/<text>
  signed bytes = "<room>|<nonce>|<text>" (utf-8)
  sig          = ed25519 sign, base64url, padding stripped
"""
import base64
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from tc_did import did_to_pubkey


def verify(did: str, sig_b64url: str, room: str, nonce: str, text: str) -> bool:
    pub = ed25519.Ed25519PublicKey.from_public_bytes(did_to_pubkey(did))
    msg = f"{room}|{nonce}|{text}".encode()
    sig = base64.urlsafe_b64decode(sig_b64url + "=" * (-len(sig_b64url) % 4))
    try:
        pub.verify(sig, msg)
        return True
    except InvalidSignature:
        return False


if __name__ == "__main__":
    if len(sys.argv) != 6:
        print(__doc__)
        print("usage: tc_verify.py <did> <sig-b64url> <room> <nonce> <text>")
        sys.exit(1)
    did, sig, room, nonce, text = sys.argv[1:]
    ok = verify(did, sig, room, nonce, text)
    print("VALID" if ok else "INVALID")
    sys.exit(0 if ok else 2)
