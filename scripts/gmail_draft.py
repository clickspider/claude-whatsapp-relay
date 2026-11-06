#!/usr/bin/env python3
"""Attach files to ONE existing Gmail draft. Drafts only: this script never sends (Daniel, 6 Nov 2026, #139).

Usage: python3 -I scripts/gmail_draft.py attach <draft-id> <file> [<file> ...] [--config relay.config.toml]

Why: the Gmail connector makes text drafts, but it cannot carry a big file. Make the draft there first, then run
this. It reads the draft (raw MIME), adds each file as an attachment and saves it back with drafts.update in the
same thread. To, Cc, Subject, the text and the files already on it stay as they are.
Files: only real files inside mail.attach_dir (relay.config.toml; no setting = nothing is attached), symlinks
resolved first, at most MAX_MB together. Uses the gmail.compose token of scripts/gmail_send.py (Keychain
mail.send_keychain_service, made by 'scripts/gmail_auth.py <config> --send'). The draft changes, so a later
'gmail_send.py send' refuses until it is shown again: the owner always sees the version with the files."""
import base64, json, mimetypes, os, sys, tomllib, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail_send as gs  # noqa: E402  same config, token and draft-id rules

MAX_MB = 10  # ponytail: JSON drafts.update, no resumable upload; raise it with the upload endpoint if needed


def allowed(paths, root):
    """Real paths of the files, or exit when one is missing, outside root, or the total is too big."""
    if not root:
        sys.exit("NOT ATTACHED: mail.attach_dir is not set in relay.config.toml.")
    root = os.path.realpath(os.path.expanduser(root))
    out = []
    for p in paths:
        real = os.path.realpath(os.path.expanduser(p))
        if os.path.commonpath([real, root]) != root:
            sys.exit(f"NOT ATTACHED: {p} is outside {root}.")
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


def main(argv):
    conf = gs.CONF
    if "--config" in argv:
        i = argv.index("--config")
        conf, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    if len(argv) < 3 or argv[0] != "attach" or not gs.ID_RE.match(argv[1]):
        sys.exit(__doc__)
    did = argv[1]
    conf = os.path.abspath(os.path.expanduser(conf))
    with open(conf, "rb") as f:
        root = tomllib.load(f).get("mail", {}).get("attach_dir", "")
    files = allowed(argv[2:], root and os.path.join(os.path.dirname(conf), os.path.expanduser(root)))
    m, here, _ = gs.conf_paths(conf)
    tok = gs.access(m, here)
    d = gs.api(tok, f"drafts/{did}?format=raw")["message"]
    msg = gs.email.message_from_bytes(base64.urlsafe_b64decode(d["raw"] + "=" * (-len(d["raw"]) % 4)),
                                      policy=gs.email.policy.default)
    have = {p.get_filename() for p in msg.walk() if p.get_filename()}
    for p in files:
        name = os.path.basename(p)
        if name in have:
            sys.exit(f"NOT ATTACHED: the draft already has {name}. Nothing changed.")
        kind = (mimetypes.guess_type(name)[0] or "application/octet-stream").split("/")
        with open(p, "rb") as f:
            msg.add_attachment(f.read(), maintype=kind[0], subtype=kind[1], filename=name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"id": did, "message": {"raw": raw}}
    if d.get("threadId"):
        body["message"]["threadId"] = d["threadId"]
    saved = put(tok, did, body)
    new_id = saved.get("id", did)
    # Check, don't trust: read the saved draft back and list its files.
    _, back = gs.draft(tok, new_id)
    names = [p.get_filename() for p in back.walk() if p.get_filename()]
    missing = [os.path.basename(p) for p in files if os.path.basename(p) not in names]
    if missing:
        sys.exit(f"CHECK FAILED: draft {new_id} saved, but {', '.join(missing)} is not on it. Look in Gmail.")
    print(f"ATTACHED to draft {new_id}: {', '.join(os.path.basename(p) for p in files)}. "
          f"Files on it now: {', '.join(names)}. Not sent.")


if __name__ == "__main__":
    main(sys.argv[1:])
