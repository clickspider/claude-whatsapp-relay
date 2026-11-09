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
      The owner sees only what THIS mail adds (Daniel 9 Nov 2026 17:15, #180): the new text and the new files.
      The quoted earlier mails and the files that came with them get one line each, and only when the mail
      watch's read-only token proves them old (every quoted line is in an earlier mail of the thread, every
      hidden file has the bytes of a file in one). Anything not proven is shown. The hash, and so the vote,
      still covers the whole mail.
Access: a separate gmail.compose refresh token in the Keychain (mail.send_keychain_service, made by
'scripts/gmail_auth.py <config> --send'); the mail watch keeps its read-only token. This script never deletes,
archives, labels or marks mail."""
import base64, contextlib, quopri, email, email.policy, email.utils, fcntl, hashlib, json, os, re, shutil, subprocess, sys, time, tomllib
from html import unescape
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


def access(m, here, read_only=False):
    """An access token: the gmail.compose one, or (read_only) the mail watch's gmail.readonly one."""
    service = (m.get("keychain_service", "relay-gmail-oauth") if read_only else
               m.get("send_keychain_service", "relay-gmail-send"))
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-a", m.get("user", ""), "-w"],
                       capture_output=True, text=True)
    if r.returncode:
        if read_only:
            raise RuntimeError("no read-only Gmail access")
        sys.exit("No send access in the Keychain: the owner runs 'scripts/gmail_auth.py <config> --send' once.")
    with open(os.path.join(here, os.path.expanduser(m.get("client_file", "gmail-client.json")))) as f:
        k = json.load(f)["installed"]
    body = urllib.parse.urlencode({"client_id": k["client_id"], "client_secret": k["client_secret"],
                                   "refresh_token": r.stdout.strip(), "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN, data=body), timeout=30) as resp:
        return json.load(resp)["access_token"]


def api(tok, path, body=None):
    """GET drafts/<id>, threads/<id>, messages/<id>, or POST drafts/send. Nothing else."""
    req = urllib.request.Request(GMAIL + path, headers={"Authorization": f"Bearer {tok}",
                                                        "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def draft(tok, did, thread=False):
    """(hash of the whole raw mail, parsed mail[, thread id])."""
    d = api(tok, f"drafts/{did}?format=raw")["message"]
    data = base64.urlsafe_b64decode(d["raw"] + "=" * (-len(d["raw"]) % 4))
    out = hashlib.sha256(data).hexdigest(), email.message_from_bytes(data, policy=email.policy.default)
    return out + (d.get("threadId", ""),) if thread else out


def parse(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=email.policy.default)


def thread_old(m, here, tid, draft_msg):
    """{"text": the earlier mails of the thread, without spaces (bare); "files": sha256 of their files}, read with the mail
    watch's read-only token. Only mails that every recipient of the draft was on count (review #180 R2: old to
    the thread is not old to them). None when there is no thread, no recipient or it cannot be read: then
    nothing is hidden."""
    rcpts = {a.lower() for _, a in email.utils.getaddresses([str(v) for h in ("To", "Cc", "Bcc") for v in draft_msg.get_all(h, [])]) if a}
    if not tid or not rcpts:
        return None
    try:
        tok = access(m, here, read_only=True)
        # ponytail: the last 30 mails, none over 5 MB (review #180 R5: show stays fast); a quote of a skipped
        # mail is not proven, so it shows in full
        ids = [x["id"] for x in api(tok, f"threads/{tid}?format=minimal").get("messages", [])
               if "DRAFT" not in x.get("labelIds", []) and x.get("sizeEstimate", 0) <= 5_000_000][-30:]
        text, files = [], set()
        for i in ids:
            mail = parse(api(tok, f"messages/{i}?format=raw")["raw"])
            had = {a.lower() for _, a in email.utils.getaddresses([str(v) for h in ("From", "To", "Cc", "Bcc") for v in mail.get_all(h, [])])}
            if not rcpts <= had:  # review #180 R2: old to the thread is not old to these recipients
                continue
            for part in mail.walk():
                data = part.get_payload(decode=True) if part.get_filename() else None
                if data:
                    files.add(hashlib.sha256(data).hexdigest())
                elif part.get_content_type() in ("text/plain", "text/html") and not part.get_filename():
                    t = part.get_content()
                    text.append(bare(strip_html(t) if part.get_content_type() == "text/html" else t))
        return {"text": "\0".join(text), "files": files}
    except Exception:  # fail open to the owner's eyes: a thread we cannot read means the full mail is shown
        return None


QUOTE_HTML = re.compile(r'<div[^>]*class="[^"]*\bgmail_(?:extra|quote)\b|<blockquote|<div[^>]*id="(?:appendonsend|'
                        r'divRplyFwdMsg)"|<div[^>]*class="moz-cite-prefix"', re.I)
QUOTE_TEXT = re.compile(r"^(?:>|On .{4,200}wrote:\s*$|Op .{4,200}(?:schreef|geschreven):\s*$|-----Original Message-----|"
                        r"בתאריך .{4,200}:\s*$)", re.M)
ATTRIBUTION = re.compile(r"^(?:On .{4,200}wrote|Op .{4,200}(?:schreef|geschreven)|בתאריך .{4,200}):$")


def split_quote(text, html, old):
    """(the new text, one line about the quote). The quote is cut off only when old proves it: its first line
    is an attribution ("On ... wrote:") and the rest is ONE unbroken piece of one earlier mail (review #180 R3:
    line by line, old words could spell a new sentence). Else the whole text stays
    and the quote line is ""."""
    plain = lambda t: tidy(strip_html(t) if html else t)
    at = (QUOTE_HTML if html else QUOTE_TEXT).search(text)
    if not at or old is None:
        return plain(text), ""
    start, head = at.start(), ""
    if not html and text[start] == ">":  # a plain attribution Gmail wrapped over two lines, just above the "> "
        head = text[:start].rstrip("\n").rsplit("\n\n", 1)[-1]
        head = head if head.count("\n") <= 1 and ATTRIBUTION.match(squash(head)) else ""
        start = text.rindex(head, 0, start) if head else start
    lines = [squash(l.lstrip("> ")) for l in (strip_html(text[start:]) if html else text[start:]).splitlines()]
    lines = ([squash(head)] + lines[head.count("\n") + 1:]) if head else lines
    lines = [l for l in lines if l]
    if not lines or not ATTRIBUTION.match(lines[0]) or "".join(map(bare, lines[1:])).replace(">", "") not in old["text"].replace(">", ""):
        return plain(text), ""
    return plain(text[:start]), f"(under it, the earlier mails are quoted: {lines[0]})"


def tidy(t):
    """One space between words, at most one empty line in a row."""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(" ".join(l.split()) for l in t.splitlines())).strip()


def file_parts(msg, old=None):
    """(the draft's file parts to show and copy, the names of files proven to come from an earlier mail)."""
    new, seen = [], []
    for p in msg.walk():
        if p.get_filename():
            data = p.get_payload(decode=True)
            if old and data and hashlib.sha256(data).hexdigest() in old["files"]:
                seen.append(p.get_filename())
            else:
                new.append(p)
    return new, seen


def describe(msg, old=None):
    """What the owner reads before the poll: only what this mail adds, when old (thread_old) proves the rest."""
    lines = [f"{h}: {msg[h]}" for h in ("To", "Cc", "Bcc", "Subject") if msg[h]]
    new, seen = file_parts(msg, old)
    if new:
        lines.append("Attachments: " + ", ".join(p.get_filename() for p in new))
    if seen:
        lines.append(f"(also on it, from an earlier mail of this thread, not sent to you again: {', '.join(seen)})")
    body = msg.get_body(preferencelist=("plain", "html"))
    text, quoted = (split_quote(body.get_content(), body.get_content_type() == "text/html", old) if body
                    else ("", ""))
    html = msg.get_body(preferencelist=("html",))
    if html and html is not body:
        h_text = split_quote(html.get_content(), True, old)[0]  # an html quote not proven old stays in it whole
        if squash(h_text) != squash(text):
            # review #139 P2-6: most mail apps show the html part; the owner must see it when it says something else
            text += "\n\n(HTML version, what most mail apps show:)\n" + h_text
    return "\n".join(lines) + "\n\n" + text.strip() + (f"\n\n{quoted}" if quoted else "")


def strip_html(t):
    """Tags out (a line break where the mail breaks a line), entities decoded (review short round P2-3)."""
    return unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</(?:div|p|li|tr|blockquote|h\d)>", "\n", t)))


def squash(t):
    return " ".join(t.split())


def bare(t):
    """No spaces at all: Gmail puts a quote's tags, and so its spaces, in other places than the mail it quotes."""
    return "".join(t.split())


def save_files(msg, folder, old=None):
    """review #139 P2-5 (decision 14:27): the draft's own NEW attachments, copied for the owner to get before
    the poll. A file proven to come from an earlier mail of the thread is not sent to him again (#180)."""
    out = []
    for i, part in enumerate(file_parts(msg, old)[0]):
        name = os.path.basename(part.get_filename()).lstrip(".") or "file"
        path = os.path.join(folder, str(i), name)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        # review short round P2-2: a forwarded mail (message/rfc822) has no decoded payload; save the mail itself
        inner = part.get_payload() if part.get_content_type() == "message/rfc822" else None
        cte = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
        data = (part.get_payload(decode=True) or b"" if not (isinstance(inner, list) and inner) else
                base64.b64decode(inner[0].get_payload()) if cte == "base64" else  # review #148 N6: the parser keeps
                quopri.decodestring(inner[0].get_payload().encode()) if cte == "quoted-printable" else  # it encoded
                inner[0].as_bytes())
        with open(path, "wb") as f:
            f.write(data)
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
    h, msg, tid = draft(tok, did, thread=True)
    if cmd == "show":
        old = thread_old(m, here, tid, msg)
        text = describe(msg, old)
        files_dir = os.path.join(state, "mail-send", "files")  # = relay MAIL_FILES
        if os.path.islink(files_dir):  # review #148 N5: never clean (or write) through a planted link
            sys.exit("NOT SHOWN: mail-send/files is a link, not a folder. Nothing changed.")
        for gone in os.listdir(files_dir) if os.path.isdir(files_dir) else ():  # one slot: older copies are void
            if gone != h:  # review short round P2-4: copies never cleaned
                shutil.rmtree(os.path.join(files_dir, gone), ignore_errors=True)
        files = save_files(msg, os.path.join(files_dir, h), old)
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
