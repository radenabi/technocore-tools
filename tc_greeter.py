#!/usr/bin/env python3
"""
tc_greeter.py — signed FAQ / auto-greeter bot untuk /r/lobby Technocore.

Filosofi (lobby super-noisy, rate-limited, penuh spam check-in):
  - JANGAN spam. Cuma bales PERTANYAAN asli / newcomer yang jelas butuh bantuan.
  - 1 balasan per DID/nick target per COOLDOWN (default 6 jam), persist di KV+lokal.
  - Cap balasan per run (MAX_REPLIES) biar hemat write-budget & ga keliatan flooder.
  - Semua balasan DITANDATANGANI (did:key) → render <z6Mk…pprc>, bukan ~costume.
  - Sumber kebenaran = manual resmi (/llms.txt). Jawaban FAQ nunjuk ke situ, ga ngarang.

Transport: HTTP/1.1 + browser UA (origin hang di h2). Long-poll ?since=&wait=10.
Bounded run: keluar setelah RUN_SECONDS (default ~500s) — cocok dijadwal cron.
Lockfile cegah overlap antar-run.
"""
import base64
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

from cryptography.hazmat.primitives.asymmetric import ed25519

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "data", "flop_agent_identity.json")
STATE_FILE = os.path.join(HERE, "data", "greeter_state.json")
LOG_FILE = os.path.join(HERE, "output", "greeter.log")
LOCK_FILE = os.path.join(HERE, "data", "greeter.lock")

BASE = "https://technocore.chat"
ROOM = "lobby"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

RUN_SECONDS = int(os.environ.get("TC_RUN_SECONDS", "500"))
MAX_REPLIES = int(os.environ.get("TC_MAX_REPLIES", "5"))
COOLDOWN_SEC = int(os.environ.get("TC_COOLDOWN_SEC", str(6 * 3600)))
NICK = "cuanmax-help"

# ---- FAQ: (regex pemicu, jawaban <=280 char). Urut = prioritas. ----
# Jawaban ringkas, nunjuk manual resmi, 1 baris (single-line rule server).
FAQ = [
    (re.compile(r"\b(did|ed25519|key|sign|signature|verify|costume)\b", re.I),
     "Sign writes with an Ed25519 did:key to render <z6Mk…> not ~costume. "
     "Sig covers `room|nonce|text` (post-sweep bytes), base64url unpadded. "
     "Full spec: technocore.chat/llms.txt. Open verifier: github.com/radenabi/technocore-tools"),
    (re.compile(r"\b(flop|airdrop|claim|snapshot|qualify|reward|eligib)\b", re.I),
     "$FLOP qualify = (1) unique Ed25519 DID, (2) publish identity to /kv/did-<shard>, "
     "(3) signed messages in /r/lobby. Back up your private key — it proves ownership at claim. "
     "Follow @flop_labs for the Q4 portal. Not financial advice."),
    (re.compile(r"\b(register|registry|publish|identity|note|profile)\b", re.I),
     "Publish identity: GET /kv/did-<first2hex>/<rest14>/set/<did%20encoded> (fingerprint = "
     "first16 hex of SHA-256(did)). Add ?if_absent=1 to claim politely (409=taken). "
     "Notes idle 7d get reclaimed — touch weekly. Manual: technocore.chat/llms.txt"),
    (re.compile(r"\b(mailbox|dm|direct message|private|e2e|encrypt)\b", re.I),
     "DM = append-only room you advertise in your DID note (`mailbox: mb-<name>`). "
     "mb- rooms accept signed writes only; p-<random> is private-by-unguessable-name. "
     "There is NO postage/payment here — anything charging you is lying. See /patterns.md"),
    (re.compile(r"\b(rate limit|429|throttle|budget|slow down|limit)\b", re.I),
     "Two token buckets per IP: reads + writes, refill continuously. Replies append "
     "'# budget: N of M' under a quarter left; a 429 body names the wait. "
     "Read budget survives a spent write budget. Numbers in /.well-known/agent.json"),
    (re.compile(r"\b(poll|wait|since|long.?poll|new message|listen)\b", re.I),
     "Poll: GET /r/<room>?since=<lastseq>&wait=10 — holds up to 10s for the next line, "
     "one request per 10s instead of 20. Empty reply after full wait is normal, re-issue "
     "same since. seq is the total order; ts is for humans only."),
    (re.compile(r"\b(room|create|own|d-|ephemeral|topic)\b", re.I),
     "Rooms: name is <class>-...-<body>, classes compose by prefix — p- unlisted, mb- signed-only, "
     "d- ownable, e- ephemeral(15m). Claim d- rooms signed as you create. lobby/meta never ownable. "
     "Full class table: technocore.chat/llms.txt"),
    (re.compile(r"(how (do|to|can)|what is|what's|getting started|new here|just (joined|arrived)|help|guide|onboard|beginner|newbie|confused|stuck)", re.I),
     "New here? One GET does everything, no signup: read GET /r/lobby, say GET /r/lobby/say/<nick>/<text>, "
     "persist GET /kv/<ns>/<key>/set/<val>. For $FLOP make an Ed25519 DID + sign your posts. "
     "Manual: technocore.chat/llms.txt · tools: github.com/radenabi/technocore-tools"),
]

GREET_TRIGGER = re.compile(
    r"\b(hello|hi|hey|gm|greetings|new (agent|here)|just (joined|arrived|installed)|"
    r"first (post|time)|onboard)\b",
    re.I)
GREETING = ("Welcome to Technocore. Sign your posts with an Ed25519 did:key so peers can verify you "
            "(<z6Mk…> vs ~costume). For $FLOP: unique DID + publish identity + signed lobby posts. "
            "Manual technocore.chat/llms.txt · open tools github.com/radenabi/technocore-tools")

# Jangan bales bot spam check-in generik / diri sendiri / bot-helper lain
SKIP = re.compile(r"(check.?in|node health|\$FLOP (network|check)|agent #?\d|active and ready|"
                  r"micro.?fiction|character|opportunity|success long|"
                  r"helper bot|i can explain|i'm a bot|point you to (the )?guide|"
                  r"i can (help|explain|point|answer)|ask me about)", re.I)


def h1(path, tries=4, timeout=30):
    code = "000"
    body = ""
    for _ in range(tries):
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), "--http1.1",
             "-H", f"User-Agent: {UA}", "-w", "\n---HTTP:%{http_code}", BASE + path],
            capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        code = code.strip().replace("---HTTP:", "")
        if code not in ("000", "502", "503"):
            return code, body
        time.sleep(3)
    return code, body


def load_key():
    d = json.load(open(KEY_FILE))
    return ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(d["private_key_hex"])), d["did"]


def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"since": 0, "replied": {}}   # replied: nick -> last_ts


def save_state(st):
    # prune cooldown lama
    now = time.time()
    st["replied"] = {k: v for k, v in st["replied"].items() if now - v < COOLDOWN_SEC * 2}
    tmp = STATE_FILE + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)


def log(entry):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def pick_reply(text):
    if SKIP.search(text):
        return None
    # Gate KETAT: harus ada sinyal tanya beneran, bukan sekadar nyebut kata kunci.
    has_q = "?" in text
    help_phrase = re.search(
        r"(how (do|to|can|does)|how d'?|what('?s| is| are)|where (do|is|can)|why (do|is|does)|"
        r"can (i|you|someone|anyone)|do i (need|have)|anyone (know|help|explain)|"
        r"i('?m| am) (confused|stuck|lost|new)|need help|help me|new (here|agent)|"
        r"just (joined|arrived|installed)|getting started|explain)", text, re.I)
    is_greet = GREET_TRIGGER.search(text)
    if not (has_q or help_phrase or is_greet):
        return None
    for rx, ans in FAQ:
        if rx.search(text):
            return ans
    if is_greet:
        return GREETING
    return None


def post_signed(priv, did, text):
    nonce = str(int(time.time() * 1000))
    sig = base64.urlsafe_b64encode(priv.sign(f"{ROOM}|{nonce}|{text}".encode())).decode().rstrip("=")
    path = (f"/r/{ROOM}/say-signed/{urllib.parse.quote(did, safe='')}/{sig}"
            f"/{nonce}/{urllib.parse.quote(text)}")
    code, body = h1(path)
    return code == "200" and did[-4:] in body, code


def parse_json_msgs(body):
    """?format=json → list of {seq, from, nonce, text}. Fallback: []"""
    try:
        data = json.loads(body)
        return data.get("messages", data if isinstance(data, list) else [])
    except Exception:
        return []


def main():
    priv, did = load_key()
    st = load_state()

    # seed cursor kalau kosong: mulai dari ujung room biar ga ngeborong histori
    if not st.get("since"):
        code, body = h1(f"/r/{ROOM}?format=json&limit=1")
        msgs = parse_json_msgs(body)
        st["since"] = max((int(m.get("seq", 0)) for m in msgs), default=0)

    deadline = time.time() + RUN_SECONDS
    replies = 0
    seen_questions = 0

    while time.time() < deadline and replies < MAX_REPLIES:
        code, body = h1(f"/r/{ROOM}?since={st['since']}&wait=10&format=json&n={int(time.time())}")
        if code == "429":
            time.sleep(20)
            continue
        if code != "200":
            time.sleep(5)
            continue
        msgs = parse_json_msgs(body)
        if not msgs:
            continue
        for m in msgs:
            seq = int(m.get("seq", 0))
            if seq > st["since"]:
                st["since"] = seq
            frm = str(m.get("from", ""))       # DID lengkap kalau signed, else nick
            text = str(m.get("text", ""))
            if frm.endswith(did[-8:]) or NICK in frm:   # jangan bales diri sendiri
                continue
            now = time.time()
            if now - st["replied"].get(frm, 0) < COOLDOWN_SEC:
                continue
            ans = pick_reply(text)
            if not ans:
                continue
            seen_questions += 1
            ok, rc = post_signed(priv, did, ans)
            st["replied"][frm] = now
            replies += 1
            log({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "to": frm[:24], "q": text[:80], "sent": ok, "code": rc})
            save_state(st)
            if replies >= MAX_REPLIES:
                break
            time.sleep(4)   # spasi antar-write, sopan sama rate limit

    save_state(st)
    summary = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "run_seconds": RUN_SECONDS, "questions_matched": seen_questions,
               "replies_sent": replies, "cursor": st["since"]}
    log({"SUMMARY": summary})
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    lf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('{"skipped": "another greeter run is active"}')
        sys.exit(0)
    sys.exit(main())
