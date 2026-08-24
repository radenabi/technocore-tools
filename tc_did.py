#!/usr/bin/env python3
"""tc_did.py — encode/decode did:key:z6Mk... ed25519 identities. Stdlib-only."""
import base64
import sys

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    res = []
    while n > 0:
        n, r = divmod(n, 58)
        res.append(B58[r])
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + "".join(reversed(res))


def did_to_pubkey(did: str) -> bytes:
    """did:key:z<base58(0xed01 || pubkey32)> -> raw 32-byte ed25519 public key."""
    if not did.startswith("did:key:z"):
        raise ValueError("not an ed25519 did:key")
    data = b58decode(did[len("did:key:z"):])
    if data[:2] != b"\xed\x01":
        raise ValueError(f"unexpected multicodec prefix: {data[:2].hex()}")
    if len(data) != 34:
        raise ValueError(f"expected 34 bytes (2 prefix + 32 key), got {len(data)}")
    return data[2:]


def pubkey_to_did(pub: bytes) -> str:
    return "did:key:z" + b58encode(b"\xed\x01" + pub)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        print("usage: tc_did.py <did-or-hex-pubkey>")
        sys.exit(1)
    arg = sys.argv[1]
    if arg.startswith("did:key:"):
        pk = did_to_pubkey(arg)
        print(f"did       : {arg}")
        print(f"pubkey hex: {pk.hex()}")
        print(f"round-trip: {pubkey_to_did(pk)}")
        assert pubkey_to_did(pk) == arg
    else:
        print(pubkey_to_did(bytes.fromhex(arg)))
