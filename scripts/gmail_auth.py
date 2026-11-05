#!/usr/bin/env python3
"""One-time Gmail access for the relay's mail watch (read only).

Usage: python3 scripts/gmail_auth.py /path/to/relay.config.toml [client_secret.json]
       python3 scripts/gmail_auth.py /path/to/relay.config.toml --send
--send: a second, separate access with gmail.compose (read and send drafts) for scripts/gmail_send.py, saved under
mail.send_keychain_service. The mail watch keeps its read-only access.

1. Takes the OAuth client file (Google Cloud > Google Auth Platform > Clients > Desktop app > Download JSON):
   the path you give, else the newest ~/Downloads/client_secret_*.json. It moves the file to mail.client_file
   (next to the config, chmod 600), so it does not stay in Downloads.
2. Opens the browser at Google's Allow screen. It asks for gmail.readonly only (read, never send or change),
   with PKCE and a redirect to this computer (127.0.0.1).
3. Checks that Google gave only that right, for the mail.user account, then saves the refresh token in the
   macOS Keychain (mail.keychain_service). The token is never printed or written to a file.
Run it again at any time to renew the access. To take the access away: myaccount.google.com/permissions."""
import base64, glob, hashlib, http.server, json, os, secrets, shutil, socketserver, subprocess, sys, time
import tomllib
import urllib.parse, urllib.request, webbrowser

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.compose"  # drafts.send refuses gmail.send
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
PROFILE = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


def main(argv):
    send = "--send" in argv
    argv = [a for a in argv if a != "--send"]
    if not argv:
        sys.exit(__doc__)
    scope, right = (SEND_SCOPE, "gmail.compose") if send else (SCOPE, "gmail.readonly")
    conf = os.path.abspath(os.path.expanduser(argv[0]))
    with open(conf, "rb") as f:
        m = tomllib.load(f).get("mail", {})
    user = m.get("user") or sys.exit("mail.user is empty in the config.")
    service = m.get("send_keychain_service", "relay-gmail-send") if send else m.get("keychain_service", "relay-gmail-oauth")
    dest = os.path.join(os.path.dirname(conf), os.path.expanduser(m.get("client_file", "gmail-client.json")))
    src = argv[1] if len(argv) > 1 else max(glob.glob(os.path.expanduser("~/Downloads/client_secret_*.json")),
                                             key=os.path.getmtime, default="")
    if src:
        shutil.move(os.path.expanduser(src), dest)
        os.chmod(dest, 0o600)
        print(f"Client file moved to {dest}")
    if not os.path.exists(dest):
        sys.exit("No client file. Download the Desktop app JSON in Google Cloud > Google Auth Platform > Clients.")
    with open(dest) as f:
        c = json.load(f).get("installed") or sys.exit("This is not a Desktop app client file.")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, got = secrets.token_urlsafe(16), {}

    class Back(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:  # only Google's answer to this run counts
                got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. Close this tab and look at the Terminal." if got else b"Wrong page.")

        def log_message(self, *a):
            pass

    class Local(http.server.HTTPServer):
        def server_bind(self):  # skip HTTPServer's reverse DNS lookup: it can hang 35 s on a Mac
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    srv = Local(("127.0.0.1", 0), Back)
    srv.timeout = 5
    redirect = f"http://127.0.0.1:{srv.server_port}"
    print(f"Opening the browser. Pick {user}, then press Allow (if Google warns that the app is not "
          "verified: Advanced > Go to the app; it is your own app).")
    webbrowser.open(AUTH + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": scope,
        "access_type": "offline", "prompt": "consent", "state": state, "login_hint": user,
        "code_challenge": challenge, "code_challenge_method": "S256"}))
    end = time.time() + 1800  # 30 min: the Allow ask goes to the owner over WhatsApp
    while not got and time.time() < end:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        sys.exit(f"No Allow: {got.get('error', 'no answer within 30 minutes')}. Nothing saved.")

    body = urllib.parse.urlencode({"code": got["code"], "client_id": c["client_id"],
                                   "client_secret": c.get("client_secret", ""), "redirect_uri": redirect,
                                   "grant_type": "authorization_code", "code_verifier": verifier}).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN, data=body), timeout=30) as r:
        tok = json.load(r)
    if set(tok.get("scope", "").split()) != {scope}:
        sys.exit(f"Google gave other rights than {right} ({tok.get('scope')}). Nothing saved.")
    if not tok.get("refresh_token"):
        sys.exit("Google sent no refresh token. Run this again. Nothing saved.")
    req = urllib.request.Request(PROFILE, headers={"Authorization": f"Bearer {tok['access_token']}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        email = json.load(r).get("emailAddress", "")
    if email.lower() != user.lower():
        sys.exit(f"You picked {email}, but mail.user is {user}. Nothing saved. Run this again.")
    # ponytail: `security -w <token>` shows the token in the process list for a moment; fine on a one-user Mac
    subprocess.run(["security", "add-generic-password", "-U", "-s", service, "-a", user, "-w", tok["refresh_token"]],
                   check=True)
    print(f"OK: {right} access for {email} is saved in the Keychain ({service}). " +
          ("Claude can now send a draft after your OK." if send else "The mail watch starts within 5 minutes."))


if __name__ == "__main__":
    main(sys.argv[1:])
