#!/usr/bin/env python3
"""Attach files to ONE existing Gmail draft, only after the owner's OK on those recipients and those exact files,
given BEFORE the upload (Daniel + Instinct, 6 Nov 2026 12:25, #139). Drafts only: this script never sends.

Usage: python3 -I scripts/gmail_draft.py show   <draft-id> <file> [<file> ...] [--config relay.config.toml]
       python3 -I scripts/gmail_draft.py attach <draft-id> <file> [<file> ...] [--config relay.config.toml]

Why a gate here too: an upload to a draft already moves the file out of the dossier, even when it is never sent,
and "an address I mailed before" is no permission for a file. So, like gmail_send.py:
show:   prints the draft's To, Cc, Bcc and the files (name, size, place), and remembers exactly that (recipients,
        file paths and a hash of each file's content). Only the newest show waits for an OK.
attach: uploads only when the owner wrote after the show, at most gs.OK_HOURS later, a short clear OK
        (gs.is_ok), the recipients and files are still exactly the shown ones (any change = show again), and that
        OK did not already unlock another attach. Then: reads the draft (raw MIME), adds the files, saves it with
        drafts.update in the same thread and reads it back. Every attach gets one line in mail-attach/attach.log.
The changed draft needs a new 'gmail_send.py show' and a NEW OK before any send: show -> OK -> send stays the
last gate, and one OK never unlocks both an attach and a send.
Files: only real files inside mail.attach_dir (no setting = nothing is attached), symlinks resolved first, never
hidden paths (.git, .env) or mail.attach_deny matches, at most MAX_MB together. Uses the gmail.compose token of
scripts/gmail_send.py (made by 'scripts/gmail_auth.py <config> --send')."""
import base64, email.utils, fnmatch, hashlib, json, mimetypes, os, sys, time, tomllib, urllib.request

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


def recipients(msg):
    """Sorted, lower-case addresses in To, Cc and Bcc."""
    return sorted({a.lower() for _, a in email.utils.getaddresses(
        [str(msg[h]) for h in ("To", "Cc", "Bcc") if msg[h]]) if a})


def fingerprint(did, msg, files):
    """What the owner's OK covers: this draft, these recipients, these exact files (path + content)."""
    h = hashlib.sha256(json.dumps([did, recipients(msg)]).encode())
    for p in files:
        with open(p, "rb") as f:
            h.update(p.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    return h.hexdigest()


def main(argv):
    conf = gs.CONF
    if "--config" in argv:
        i = argv.index("--config")
        conf, argv = argv[i + 1], argv[:i] + argv[i + 2:]
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
    pending_path, log_path = (os.path.join(state, "mail-attach", n) for n in ("pending.json", "attach.log"))
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
    key = fingerprint(did, msg, files)
    if cmd == "show":
        gs.save(pending_path, {"draft": did, "key": key, "time": time.time()})  # only the newest show waits
        print("\n".join(f"{h}: {msg[h]}" for h in ("To", "Cc", "Bcc", "Subject") if msg[h]))
        print("Files to attach:")
        for p in files:
            print(f"  {os.path.basename(p)} ({os.path.getsize(p) // 1024} KB) from {p}")
        print(f"\nNothing is uploaded yet. Show the owner exactly these recipients and files and ask, naming them. "
              f"Attach only after the owner's next message is a clear OK: gmail_draft.py attach {did} <same files>")
        return
    p = gs.load(pending_path, {})
    ok = gs.load(os.path.join(state, "owner-last.json"), {})
    why = ("these files were not shown to the owner first (run show)" if p.get("draft") != did else
           "the recipients or files changed after the show: run show again and ask again" if p.get("key") != key else
           "the owner wrote nothing after the show" if ok.get("time", 0) <= p.get("time", 0) else
           "the OK came too late after the show: run show again" if ok["time"] - p["time"] > gs.OK_HOURS * 3600 else
           "the owner's last message is not a clear OK" if not gs.is_ok(ok.get("text")) else
           "that OK already attached files once: one OK = one attach" if p.get("used") == ok["time"] else "")
    if why:
        sys.exit(f"NOT ATTACHED: {why}.")
    gs.save(pending_path, dict(p, used=ok["time"]))  # before the upload: one OK can never upload twice
    for f_ in files:
        name = os.path.basename(f_)
        kind = (mimetypes.guess_type(name)[0] or "application/octet-stream").split("/")
        with open(f_, "rb") as f:
            msg.add_attachment(f.read(), maintype=kind[0], subtype=kind[1], filename=name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"id": did, "message": {"raw": raw}}
    if d.get("threadId"):
        body["message"]["threadId"] = d["threadId"]
    line = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "draft": did, "to": recipients(msg),
            "files": files, "ok": ok.get("text", "")}
    os.makedirs(os.path.dirname(log_path), mode=0o700, exist_ok=True)
    try:
        new_id = put(tok, did, body).get("id", did)
    except Exception as e:  # a timeout can come after Gmail saved it: never say ATTACHED, never retry here
        with open(log_path, "a") as f:
            f.write(json.dumps(dict(line, outcome=f"UNKNOWN: {e}"), ensure_ascii=False) + "\n")
        sys.exit(f"OUTCOME UNKNOWN: the upload to draft {did} failed ({e}). Look in Gmail before you try again "
                 "(that needs a new show and a new OK).")
    with open(log_path, "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    # Check, don't trust: read the saved draft back and list its files.
    _, back = gs.draft(tok, new_id)
    names = [q.get_filename() for q in back.walk() if q.get_filename()]
    missing = [os.path.basename(q) for q in files if os.path.basename(q) not in names]
    if missing:
        sys.exit(f"CHECK FAILED: draft {new_id} saved, but {', '.join(missing)} is not on it. Look in Gmail.")
    print(f"ATTACHED to draft {new_id}: {', '.join(os.path.basename(q) for q in files)}. "
          f"Files on it now: {', '.join(names)}. Not sent: a send needs gmail_send.py show and a new OK.")


if __name__ == "__main__":
    main(sys.argv[1:])
