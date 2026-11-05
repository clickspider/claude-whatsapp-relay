#!/usr/bin/env python3
"""Send ONE existing Gmail draft, only after the owner's OK on exactly that draft (rule 5 Nov 2026).

Usage: python3 -I scripts/gmail_send.py show <draft-id> [relay.config.toml]
       python3 -I scripts/gmail_send.py send <draft-id> [relay.config.toml]

show: prints To, Cc, Bcc, Subject, attachments and the text, and remembers this exact version (a hash of the
      whole mail). Only the newest shown draft waits for an OK. Show the owner what it prints, then ask.
send: sends it (Gmail drafts.send) only when all of this holds, else nothing goes out:
      1. the owner wrote after the show, at most OK_HOURS later, and that message is a short clear OK
         ('שלח', 'ok send', 'כן') with no 'no', 'wait', 'but' or change in it;
      2. the draft is still exactly the version that was shown (any edit = show it again);
      3. that OK did not already send another mail: one OK = one mail.
      Every send gets one line in <state_dir>/mail-send/sent.log (time, draft id, to, cc, subject).
The relay writes the owner's last message to <state_dir>/owner-last.json. Access: a separate gmail.compose
refresh token in the Keychain (mail.send_keychain_service, made by 'scripts/gmail_auth.py <config> --send');
the mail watch keeps its read-only token. This script never deletes, archives, labels or marks mail."""
import base64, email, email.policy, hashlib, json, os, re, subprocess, sys, time, tomllib
import urllib.parse, urllib.request

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/"
TOKEN = "https://oauth2.googleapis.com/token"
CONF = "~/projects/whatsapp-claude-relay/relay.config.toml"
ID_RE = re.compile(r"^r-?\d{6,25}$")  # Gmail draft ids: 'r123...' or 'r-123...'
OK_HOURS = 2
MAX_OK_WORDS = 8
OK_WORDS = {"ok", "okay", "oki", "okey", "yes", "yep", "yup", "send", "go", "sure", "כן", "שלח", "תשלח", "לשלוח",
            "שלחו", "אשר", "מאשר", "מאושר", "אוקיי", "אוקי", "סבבה", "יאללה", "בסדר"}
NO_WORDS = {"no", "nope", "not", "don", "dont", "don't", "cancel", "wait", "stop", "hold", "later", "but", "change",
            "fix", "edit", "before",
            "אל", "לא", "עוד", "חכה", "תחכה", "רגע", "אבל", "תשנה", "שנה", "תתקן", "תקן", "לפני", "אחכ", "בלי", "בטל"}


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
        text = re.sub(r"<[^>]+>", " ", text)
    return "\n".join(lines) + "\n\n" + text.strip()


def is_ok(text):
    """A short, clear OK with no 'no', 'wait', 'but' or change request in it."""
    words = re.findall(r"[\w']+", (text or "").lower())
    return 0 < len(words) <= MAX_OK_WORDS and bool(OK_WORDS & set(words)) and not NO_WORDS & set(words)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)


def main(argv):
    if len(argv) < 2 or argv[0] not in ("show", "send") or not ID_RE.match(argv[1]):
        sys.exit(__doc__)
    cmd, did = argv[0], argv[1]
    m, here, state = conf_paths(os.path.abspath(os.path.expanduser(argv[2] if len(argv) > 2 else CONF)))
    pending_path, log_path = os.path.join(state, "mail-send", "pending.json"), os.path.join(state, "mail-send", "sent.log")
    tok = access(m, here)
    h, msg = draft(tok, did)
    if cmd == "show":
        save(pending_path, {"draft": did, "hash": h, "time": time.time()})  # only the newest shown draft waits
        print(describe(msg))
        print(f"\nShow the owner exactly this and ask. Send only after the owner's next message is a clear OK "
              f"for it: gmail_send.py send {did}")
        return
    p = load(pending_path, {})
    ok = load(os.path.join(state, "owner-last.json"), {})
    why = ("this draft was not shown to the owner first (run show)" if p.get("draft") != did else
           "the draft changed after it was shown: run show again and ask again" if p.get("hash") != h else
           "the owner wrote nothing after the show" if ok.get("time", 0) <= p.get("time", 0) else
           "the OK came too late after the show: run show again" if ok["time"] - p["time"] > OK_HOURS * 3600 else
           "the owner's last message is not a clear OK" if not is_ok(ok.get("text")) else
           "that OK already sent a mail: one OK = one mail" if p.get("used") == ok["time"] else "")
    if why:
        sys.exit(f"NOT SENT: {why}.")
    save(pending_path, dict(p, used=ok["time"]))  # before the send: a crash can never send twice on one OK
    sent = api(tok, "drafts/send", {"id": did})
    line = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "draft": did, "message": sent.get("id", ""),
            "to": str(msg["To"] or ""), "cc": str(msg["Cc"] or ""), "subject": str(msg["Subject"] or ""),
            "ok": ok.get("text", "")}
    os.makedirs(os.path.dirname(log_path), mode=0o700, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"SENT: draft {did} to {line['to']}, subject '{line['subject']}' (Gmail message {line['message']}).")


if __name__ == "__main__":
    main(sys.argv[1:])
