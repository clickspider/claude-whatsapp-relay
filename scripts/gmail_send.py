#!/usr/bin/env python3
"""Send ONE existing Gmail draft, only after the owner's vote on a poll for exactly that draft (rule 5 Nov 2026;
a poll since 6 Nov 2026 14:27: a typed "ok" never sends).

Usage: python3 -I scripts/gmail_send.py show <draft-id>
       python3 -I scripts/gmail_send.py send <draft-id>
The config is RELAY_CONFIG, set by the relay for the claude it starts; never an argument (review #139 F1, N6).

show: prints To, Cc, Bcc, Subject, attachments and the text, and puts exactly this version (a hash of the whole
      mail) in the one waiting slot, <state_dir>/mail-send/pending.json, status "requested". After the turn the
      relay (mail_gate_ask) sends the owner that text and a "Send / Don't send" poll it makes itself, and writes
      the poll id. The owner's vote on THAT poll is written by the relay (mail_gate_vote), never by Claude.
send: sends it (Gmail drafts.send) only when the slot is this draft, the draft is still exactly the shown
      version, the vote on the slot's own poll is "Send", the show is at most OK_HOURS old, and that vote did
      not already do its action (one vote = one action). A newer show of either gate replaces the slot, so an
      old vote never counts. Every send gets one line in <state_dir>/mail-send/sent.log.
Access: a separate gmail.compose refresh token in the Keychain (mail.send_keychain_service, made by
'scripts/gmail_auth.py <config> --send'); the mail watch keeps its read-only token. This script never deletes,
archives, labels or marks mail."""
import base64, contextlib, email, email.policy, fcntl, hashlib, json, os, re, subprocess, sys, time, tomllib
import urllib.parse, urllib.request

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/"
TOKEN = "https://oauth2.googleapis.com/token"
CONF = os.environ.get("RELAY_CONFIG", "")  # set by the relay for the claude it starts; never from argv (#139 F1, N6)
ID_RE = re.compile(r"^r-?\d{6,25}$")  # Gmail draft ids: 'r123...' or 'r-123...'
OK_HOURS = 2


def conf_paths(conf):
    with open(conf, "rb") as f:
        c = tomllib.load(f)
    here = os.path.dirname(conf)
    state = os.path.abspath(os.path.join(here, os.path.expanduser(c.get("paths", {}).get("state_dir", "state"))))
    return c.get("mail", {}), here, state


def access(m, here):
    r = subprocess.run(["security", "find-generic-password", "-s", m.get("send_keychain_service", "relay-gmail-send"),
                        "-a", m.get("user", ""), "-w"], capture_output=True, text=True)
    if r.returncode:
        sys.exit("No send access in the Keychain: the owner runs 'scripts/gmail_auth.py <config> --send' once.")
    with open(os.path.join(here, os.path.expanduser(m.get("client_file", "gmail-client.json")))) as f:
        k = json.load(f)["installed"]
    body = urllib.parse.urlencode({"client_id": k["client_id"], "client_secret": k["client_secret"],
                                   "refresh_token": r.stdout.strip(), "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN, data=body), timeout=30) as resp:
        return json.load(resp)["access_token"]


def api(tok, path, body=None):
    """GET drafts/<id>, or POST drafts/send. Nothing else."""
    req = urllib.request.Request(GMAIL + path, headers={"Authorization": f"Bearer {tok}",
                                                        "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def draft(tok, did):
    """(hash of the whole raw mail, parsed mail)."""
    raw = api(tok, f"drafts/{did}?format=raw")["message"]["raw"]
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    return hashlib.sha256(data).hexdigest(), email.message_from_bytes(data, policy=email.policy.default)


def describe(msg):
    lines = [f"{h}: {msg[h]}" for h in ("To", "Cc", "Bcc", "Subject") if msg[h]]
    files = [p.get_filename() for p in msg.walk() if p.get_filename()]
    if files:
        lines.append("Attachments: " + ", ".join(files))
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body and body.get_content_type() == "text/html":
        text = strip_html(text)
    html = msg.get_body(preferencelist=("html",))
    if html and html is not body and squash(strip_html(html.get_content())) != squash(text):
        # review #139 P2-6: most mail apps show the html part; the owner must see it when it says something else
        text += "\n\n(HTML version, what most mail apps show:)\n" + strip_html(html.get_content()).strip()
    return "\n".join(lines) + "\n\n" + text.strip()


def strip_html(t):
    return re.sub(r"<[^>]+>", " ", t)


def squash(t):
    return " ".join(t.split())


def save_files(msg, folder):
    """review #139 P2-5 (decision 14:27): the draft's own attachments, copied for the owner to get before the poll."""
    out = []
    for i, part in enumerate(p for p in msg.walk() if p.get_filename()):
        name = os.path.basename(part.get_filename()).lstrip(".") or "file"
        path = os.path.join(folder, str(i), name)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "wb") as f:
            f.write(part.get_payload(decode=True) or b"")
        out.append(path)
    return out


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    """tmp + fsync + rename: a crash or a reader never sees half a file (review #139 N7)."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


SEND, NO = "Send", "Don't send"  # the approval poll's options (Daniel 6 Nov 2026 14:27)


def vote_ok(p):
    """'' when the owner's vote on the relay's poll approves exactly this pending show, else why not.
    Only the relay writes "poll", "asked" and "vote" (relay.py mail_gate_ask / mail_gate_vote)."""
    v = p.get("vote") or {}
    return ("the owner was not asked yet: the relay sends the mail and the poll after your reply" if not p.get("poll") else
            "the owner has not voted on the poll yet" if not v else
            "the vote is for another poll: run show again" if v.get("poll") != p["poll"] else
            "the owner voted not to" if v.get("choice") != SEND else
            "the vote has a time in the future (clock changed?): run show again" if v.get("time", 0) > time.time() + 60 else
            "the show is too old: run show again" if time.time() - p.get("time", 0) > OK_HOURS * 3600 else
            "that vote already did its action: one vote = one action" if p.get("used") else "")


@contextlib.contextmanager
def slot_lock(path):
    """The slot's read-change-write, one at a time with the relay (same <slot>.lock, flock): a vote written
    by the relay can never bring back an older show, and a show never wipes a vote it did not see."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(os.open(path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def request(path, entry):
    """The newest show of either gate (one slot, kind send|attach). status "requested": the relay sends the
    summary, the files and a Send / Don't send poll to the owner after the turn."""
    with slot_lock(path):
        save(path, dict(entry, time=time.time(), status="requested"))


def main(argv):
    if len(argv) < 2 or argv[0] not in ("show", "send") or not ID_RE.match(argv[1]):
        sys.exit(__doc__)
    cmd, did = argv[0], argv[1]
    if len(argv) > 2:  # review #139 F1: a config from argv = a state folder the caller wrote
        sys.exit("NOT SENT: the config comes from the relay (RELAY_CONFIG); no config argument.")
    if not CONF:
        sys.exit("NOT SENT: RELAY_CONFIG is not set (run this from the relay).")
    m, here, state = conf_paths(os.path.abspath(os.path.expanduser(CONF)))
    pending_path, log_path = os.path.join(state, "mail-send", "pending.json"), os.path.join(state, "mail-send", "sent.log")  # also gmail_draft's slot
    tok = access(m, here)
    h, msg = draft(tok, did)
    if cmd == "show":
        text = describe(msg)
        files = save_files(msg, os.path.join(state, "mail-send", "files", h))  # = relay MAIL_FILES
        request(pending_path, {"kind": "send", "draft": did, "hash": h, "subject": str(msg["Subject"] or ""),
                               "files": files, "summary": "Mail to send:\n" + text})
        print(text)
        print(f"\nNothing is sent. After your reply the relay sends the owner this mail and a '{SEND} / {NO}' poll. "
              f"Do not ask in text and do not repeat the mail. A typed 'ok' never sends. Only when the vote comes back "
              f"as '{SEND}', run: gmail_send.py send {did}")
        return
    with slot_lock(pending_path):
        p = load(pending_path, {})
        why = ("this draft was not shown to the owner first (run show)" if p.get("draft") != did or p.get("kind") != "send" else
               "the draft changed after it was shown: run show again" if p.get("hash") != h else vote_ok(p))
        if why:
            sys.exit(f"NOT SENT: {why}.")
        save(pending_path, dict(p, used=True))  # before the send: a crash can never send twice on one vote
    line = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "draft": did, "to": str(msg["To"] or ""),
            "cc": str(msg["Cc"] or ""), "subject": str(msg["Subject"] or ""), "poll": p["poll"]}
    os.makedirs(os.path.dirname(log_path), mode=0o700, exist_ok=True)
    # The hash was checked a moment ago; drafts.send sends the draft as it is now. An edit in another Gmail
    # client inside that gap goes out: the API has no "send only this version" (Instinct review, known limit).
    try:
        sent = api(tok, "drafts/send", {"id": did})
    except Exception as e:  # a timeout can come after Gmail accepted it: never say SENT, never retry here
        with open(log_path, "a") as f:
            f.write(json.dumps(dict(line, outcome=f"UNKNOWN: {e}"), ensure_ascii=False) + "\n")
        sys.exit(f"OUTCOME UNKNOWN: the send of draft {did} failed ({e}). It may have gone out: check Sent in "
                 "Gmail before you send it again (that needs a new show and a new OK).")
    line["message"] = sent.get("id", "")
    with open(log_path, "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"SENT: draft {did} to {line['to']}, subject '{line['subject']}' (Gmail message {line['message']}).")


if __name__ == "__main__":
    main(sys.argv[1:])
