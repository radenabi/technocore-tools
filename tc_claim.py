#!/usr/bin/env python3
"""
tc_claim.py — hourly registry claim (ringan, 1-2 GET, TIDAK post ke lobby).
Jalan tiap jam via cron buat nyamperin slot KV yang ke-reclaim (cap 5120).
Output: 1 baris JSON. Sukses = CLAIMED / cap-full. Error jaringan = http-XXX.
"""
import hashlib
import json
import os
import subprocess
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "data", "flop_agent_identity.json")
LOG_FILE = os.path.join(HERE, "output", "claim.log")
BASE = "https://technocore.chat"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def h1(path: str, tries: int = 3):
    code = "000"
    body = ""
    for _ in range(tries):
        r = subprocess.run(
            ["curl", "-s", "--max-time", "20", "--http1.1",
             "-H", f"User-Agent: {UA}", "-w", "\n---HTTP:%{http_code}", BASE + path],
            capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        code = code.strip().replace("---HTTP:", "")
        if code not in ("000", "502", "503"):   # origin flaky -> retry cepat
            return code, body
        time.sleep(5)
    return code, body


def main() -> str:
    did = json.load(open(KEY_FILE))["did"]
    fp = hashlib.sha256(did.encode()).hexdigest()[:16]

    code, body = h1(f"/kv/did/{fp}/set/{urllib.parse.quote(did, safe='')}?if_absent=1")

    # STRICT: klaim dianggap sukses HANYA kalau readback beneran ngembaliin DID kita
    state = None
    if code == "200":
        time.sleep(2)
        rc, rb = h1(f"/kv/did/{fp}")
        if rc == "200" and did in rb:
            state = "CLAIMED+verified"
        elif rc == "200" and rb.startswith("404"):
            state = "write-200-but-not-persisted"
        elif rc == "000":
            state = "claimed-unverified(read-timeout)"
    if state is None:
        if code == "400" and "limit reached" in body:
            state = "cap-full"
        elif code == "409":
            state = "taken-by-other(409)"
        else:
            state = f"http-{code}"

    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "state": state}
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return json.dumps(entry)


if __name__ == "__main__":
    print(main())
