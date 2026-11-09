#!/usr/bin/env python3
"""Attach files to ONE existing Gmail draft, only after the owner's vote on a poll for those recipients and those
exact files, given BEFORE the upload (Daniel + Instinct 12:25, poll 14:27, 6 Nov 2026, #139). Never sends.

Usage: python3 -I scripts/gmail_draft.py show   <draft-id> <file> [<file> ...]
       python3 -I scripts/gmail_draft.py attach <draft-id> <file> [<file> ...]
The config is the relay's RELAY_CONFIG (gs.CONF), never an argument.

Why a gate here too: an upload to a draft already moves the file out of the dossier, even when it is never sent,
and "an address I mailed before" is no permission for a file.
show:   uploads nothing. Puts the draft, its recipients and the files (path + content hash, read once) in the one
        waiting slot shared with gmail_send.py (mail-send/pending.json, kind attach). After the turn the relay
        sends the owner the recipients, each file's name, size and pages, the files themselves, and its own
        "Send / Don't send" poll (gmail_send.py has the rest of the flow).
attach: only when the slot is this draft, the recipients and files are exactly the shown ones (any change =
        show again; the hashed bytes are the uploaded bytes), and gs.vote_ok passes. Then it reads the draft (raw
        MIME), adds the files, saves it with drafts.update in the same thread and reads it back. Every attach gets
        one line with its outcome in mail-attach/attach.log.
The changed draft needs its own gmail_send.py show and a new vote before any send: one vote never does both.
Files: only real files inside mail.attach_dir (no setting = nothing is attached), symlinks resolved first, never
hidden paths (.git, .env) or mail.attach_deny matches, at most MAX_MB together. Uses the gmail.compose token of
scripts/gmail_send.py (made by 'scripts/gmail_auth.py <config> --send')."""
import base64, email.utils, fnmatch, hashlib, json, mimetypes, os, re, sys, time, tomllib, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail_send as gs  # noqa: E402  same config, token and draft-id rules

MAX_MB = 10  # ponytail: JSON drafts.update, no resumable upload; raise it with the upload endpoint if needed


def allowed(paths, root, deny=()):
    """Real paths of the files, or exit when one is missing, outside root, hidden (.git, .env, ...), matches a
    mail.attach_deny pattern (relative to root), or the total is too big."""
    if not root:
        sys.exit("NOT ATTACHED: mail.attach_dir is not set in relay.config.toml.")
    root = os.path.realpath(os.path.expanduser(root))
    out = []
    for p in paths:
        real = os.path.realpath(os.path.expanduser(p))
        if os.path.commonpath([real, root]) != root:
            sys.exit(f"NOT ATTACHED: {p} is outside {root}.")
        rel = os.path.relpath(real, root)
        if any(part.startswith(".") for part in rel.split(os.sep)):
            sys.exit(f"NOT ATTACHED: {p} is in a hidden folder or is a hidden file.")
        if any(fnmatch.fnmatchcase(rel.lower(), pat.lower()) for pat in deny):  # APFS ignores case, so do we
            sys.exit(f"NOT ATTACHED: {p} is on the attach_deny list.")
        if not os.path.isfile(real):
            sys.exit(f"NOT ATTACHED: {p} is not a file.")
        out.append(real)
    if sum(os.path.getsize(p) for p in out) > MAX_MB * 1024 * 1024:
        sys.exit(f"NOT ATTACHED: the files are bigger than {MAX_MB} MB together.")
    return out


def put(tok, did, body):
    """PUT drafts/<id> (drafts.update). The only write here."""
    req = urllib.request.Request(gs.GMAIL + f"drafts/{did}", data=json.dumps(body).encode(), method="PUT",
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def pages(data):
    """', N pages' for a PDF (its /Type /Page objects), else ''."""
    if not data.startswith(b"%PDF"):
        return ""
    n = len(re.findall(rb"/Type\s*/Page(?!s)", data))
    return f", {n} page{'s' if n != 1 else ''}" if n else ""


def cut(t, n=1500):
    """The first n characters, with a mark when there was more (review short round P2-4)."""
    return t[:n] + ("\n(... cut: the rest is in the draft)" if len(t) > n else "")


def recipients(msg):
    """Sorted, lower-case addresses in To, Cc and Bcc."""
    return sorted({a.lower() for _, a in email.utils.getaddresses(
        [str(msg[h]) for h in ("To", "Cc", "Bcc") if msg[h]]) if "@" in a})


def fingerprint(did, msg, files):
    """What the owner's OK covers: this draft, these recipients, these exact files (path + content)."""
    h = hashlib.sha256(json.dumps([did, recipients(msg)]).encode())
    for p in files:
        h.update(p.encode() + b"\0" + hashlib.sha256(files[p]).digest())
    return h.hexdigest()


def main(argv):
    conf = gs.CONF  # the relay's RELAY_CONFIG, never a config path from argv (review #139 F1, N6)
    if not conf:
        sys.exit("NOT ATTACHED: RELAY_CONFIG is not set (run this from the relay).")
    if len(argv) < 3 or argv[0] not in ("show", "attach") or not gs.ID_RE.match(argv[1]):
        sys.exit(__doc__)
    cmd, did = argv[0], argv[1]
    conf = os.path.abspath(os.path.expanduser(conf))
    with open(conf, "rb") as f:
        mail = tomllib.load(f).get("mail", {})
    root = mail.get("attach_dir", "")
    files = allowed(argv[2:], root and os.path.join(os.path.dirname(conf), os.path.expanduser(root)),
                    mail.get("attach_deny", []))
    m, here, state = gs.conf_paths(conf)
    pending_path = os.path.join(state, "mail-send", "pending.json")  # ONE slot with gmail_send: one OK, one action
    log_path = os.path.join(state, "mail-attach", "attach.log")
    tok = gs.access(m, here)
    d = gs.api(tok, f"drafts/{did}?format=raw")["message"]
    msg = gs.email.message_from_bytes(base64.urlsafe_b64decode(d["raw"] + "=" * (-len(d["raw"]) % 4)),
                                      policy=gs.email.policy.default)
    if not recipients(msg):
        sys.exit("NOT ATTACHED: the draft has no recipient yet. The OK must cover who gets the files.")
    have = {p.get_filename() for p in msg.walk() if p.get_filename()}
    for p in files:
        if os.path.basename(p) in have:
            sys.exit(f"NOT ATTACHED: the draft already has {os.path.basename(p)}. Nothing changed.")
    data = {}
    for p in files:  # read once: the hashed bytes are the uploaded bytes (review #139 F5)
        with open(p, "rb") as f:
            data[p] = f.read()
    files = data
    key = fingerprint(did, msg, files)
    if cmd == "show":
        head = "\n".join(f"{h}: {msg[h]}" for h in ("To", "Cc", "Bcc", "Subject") if msg[h])
        lines = [f"  {os.path.basename(p)} ({len(b)} bytes{pages(b)})" for p, b in files.items()]
        gs.request(pending_path, {"kind": "attach", "draft": did, "key": key, "subject": str(msg["Subject"] or ""),
                                  "files": list(files),
                                  "summary": f"Files to put on this draft (nothing is uploaded yet):\n{head}\n" + "\n".join(lines)
                                             + "\n\nThe mail text now:\n" + cut(gs.describe(msg, gs.thread_old(m, here, d.get("threadId"), msg)).split("\n\n", 1)[-1]),
                                  "msgid": d.get("id")})  # review F6: Gmail gives every saved edit a new id
        print(head + "\nFiles to attach:\n" + "\n".join(f"{q} from {p}" for q, p in zip(lines, files)))
        print(f"\nNothing is uploaded. After your reply the relay sends the owner these recipients, the files "
              f"themselves and a '{gs.SEND} / {gs.NO}' poll. Do not ask in text. A typed 'ok' never attaches. Only "
              f"when the vote comes back as '{gs.SEND}', run: gmail_draft.py attach {did} <same files>")
        return
    with gs.slot_lock(pending_path):
        p = gs.load(pending_path, {})
        why = ("these files were not shown to the owner first (run show)" if p.get("draft") != did or p.get("kind") != "attach" else
               "the recipients or files changed after the show: run show again" if p.get("key") != key else
               "this show is from an older version of this script: run show again" if "msgid" not in p else
               "the draft changed in Gmail after the show: run show again" if p.get("msgid") != d.get("id") else
               gs.vote_ok(p))
        if why:
            sys.exit(f"NOT ATTACHED: {why}.")
        gs.save(pending_path, dict(p, used=True))  # before the upload: one vote can never upload twice
    for f_ in files:
        name = os.path.basename(f_)
        kind = (mimetypes.guess_type(name)[0] or "application/octet-stream").split("/")
        msg.add_attachment(files[f_], maintype=kind[0], subtype=kind[1], filename=name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"id": did, "message": {"raw": raw}}
    if d.get("threadId"):
        body["message"]["threadId"] = d["threadId"]
    line = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "draft": did, "to": recipients(msg),
            "files": list(files), "poll": p["poll"]}
    os.makedirs(os.path.dirname(log_path), mode=0o700, exist_ok=True)
    # review F6: the PUT writes the whole mail as read above; a Gmail edit since then would be lost. Check
    # once more right before it (the gap left is the seconds of this call; the API has no if-match).
    try:
        why = ("the draft changed in Gmail just now" if
               gs.api(tok, f"drafts/{did}?format=minimal")["message"].get("id") != d.get("id") else "")
    except Exception as e:  # review #148 N7: logged too, never silent
        why = f"the check before the upload failed ({e})"
    if why:
        with open(log_path, "a") as f:
            f.write(json.dumps(dict(line, outcome=f"NOT ATTACHED: {why}"), ensure_ascii=False) + "\n")
        sys.exit(f"NOT ATTACHED: {why}. Nothing was uploaded: run show again.")
    try:
        new_id = put(tok, did, body).get("id", did)
    except Exception as e:  # a timeout can come after Gmail saved it: never say ATTACHED, never retry here
        with open(log_path, "a") as f:
            f.write(json.dumps(dict(line, outcome=f"UNKNOWN: {e}"), ensure_ascii=False) + "\n")
        sys.exit(f"OUTCOME UNKNOWN: the upload to draft {did} failed ({e}). Look in Gmail before you try again "
                 "(that needs a new show and a new OK).")
    # Check, don't trust: read the saved draft back and list its files. The log line says what really happened.
    try:
        _, back = gs.draft(tok, new_id)
        names, outcome = [q.get_filename() for q in back.walk() if q.get_filename()], "CHECK FAILED"
    except Exception as e:
        names, outcome = [], f"OUTCOME UNKNOWN (saved, read-back failed: {e})"
    missing = [os.path.basename(q) for q in files if os.path.basename(q) not in names]
    outcome = "ATTACHED" if not missing else outcome
    with open(log_path, "a") as f:
        f.write(json.dumps(dict(line, outcome=outcome), ensure_ascii=False) + "\n")
    if missing:
        sys.exit(f"{outcome}: draft {new_id}, {', '.join(missing)} not confirmed on it. Look in Gmail.")
    print(f"ATTACHED to draft {new_id}: {', '.join(os.path.basename(q) for q in files)}. "
          f"Files on it now: {', '.join(names)}. Not sent: a send needs gmail_send.py show and a new OK.")


if __name__ == "__main__":
    main(sys.argv[1:])
