#!/usr/bin/env python3
"""Tell the owner on WhatsApp when the relay is down. Plain code, no AI, no import of relay.py
(a broken relay.py is exactly the case it must survive).

launchd runs it every minute (launchd/watchdog.plist.template). The relay touches <state>/heartbeat
about every 20 s. When the heartbeat is older than 2 minutes for [watchdog] down_minutes in a row
(a crash loop, a hang, a job that is gone), it sends ONE message straight through the bridge and
writes <state>/watchdog-down.json. The relay itself says "back" after its next start and removes
that file. Asleep (a gap between two ticks) = no alarm; owner.night (or watchdog.night) = wait for the morning.
Usage: relay_watchdog.py --config path/to/relay.config.toml"""
import datetime, json, os, sqlite3, sys, time, tomllib, urllib.request
from zoneinfo import ZoneInfo

STALE = 120  # seconds without a heartbeat before the watchdog starts counting
GAP = 180    # a gap this long between two ticks = the Mac slept: start counting again


def path(p, base):
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


def settings(conf):
    with open(conf, "rb") as f:
        raw = tomllib.load(f)
    base = os.path.dirname(os.path.abspath(conf))
    o, b, w = raw.get("owner", {}), raw.get("bridge", {}), raw.get("watchdog", {})
    return {"state": path(raw.get("paths", {}).get("state_dir", "state"), base),
            "store": path(b.get("store", ""), base), "api": b.get("api", "http://127.0.0.1:8080/api"),
            "owner": o.get("jid", ""), "tz": o.get("timezone", "UTC"), "night": w.get("night", o.get("night", [])),
            "enabled": w.get("enabled", True), "minutes": w.get("down_minutes", 3),
            "text": w.get("down_text", "The relay is down since {since} and does not answer. Log: {log}"),
            "log": path(raw.get("launchd", {}).get("log_dir", "~/Library/Logs/claude-whatsapp-relay"), base)}


def at_night(s, now):
    n = s["night"]
    if not n:
        return False
    h = datetime.datetime.fromtimestamp(now, ZoneInfo(s["tz"])).hour
    return (n[0] <= h or h < n[1]) if n[0] > n[1] else n[0] <= h < n[1]


def token(s):
    tok = os.environ.get("WHATSAPP_BRIDGE_TOKEN", "").strip()
    if not tok:
        with open(os.path.join(s["store"], ".bridge-token")) as f:
            tok = f.read().strip()
    return tok


def send(s, text):
    """Through the bridge API; returns the message id from the bridge store (the relay skips that row)."""
    tok = token(s)
    req = urllib.request.Request(f"{s['api']}/send", json.dumps({"recipient": s["owner"], "message": text}).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + tok})
    urllib.request.urlopen(req, timeout=30).read()
    try:
        c = sqlite3.connect(f"file:{os.path.join(s['store'], 'messages.db')}?mode=ro", uri=True)
        row = c.execute("SELECT id FROM messages WHERE chat_jid = ? AND is_from_me AND content = ? "
                        "ORDER BY rowid DESC LIMIT 1", (s["owner"], text)).fetchone()
        c.close()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def tick(s, now=None, sender=send):
    """One check. Returns what happened (for the log and the test)."""
    now = now or time.time()
    st = os.path.join(s["state"], "watchdog.json")
    down = os.path.join(s["state"], "watchdog-down.json")
    try:
        with open(st) as f:
            mem = json.load(f)
    except (OSError, ValueError):
        mem = {}
    hb = os.path.join(s["state"], "heartbeat")
    if not os.path.exists(hb):
        return "no heartbeat yet"  # a relay older than the watchdog, or never started: nothing to judge
    beat = os.path.getmtime(hb)
    since = mem.get("stale_since")
    if now - mem.get("last_tick", 0) > GAP or now - beat < STALE:
        since = None
    elif since is None:
        since = now
    what = "ok" if since is None else "stale"
    if since is not None and now - since >= s["minutes"] * 60 and not os.path.exists(down):
        if at_night(s, now):
            what = "night"
        else:
            t = datetime.datetime.fromtimestamp(beat, ZoneInfo(s["tz"])).strftime("%H:%M")
            text = s["text"].format(since=t, log=os.path.join(s["log"], "relay.log"))
            mid = sender(s, text)
            with open(down, "w") as f:
                json.dump({"since": beat, "at": now, "id": mid, "text": text}, f)
            what = "alerted"
    with open(st, "w") as f:
        json.dump({"last_tick": now, "stale_since": since}, f)
    return what


def main(argv):
    conf = argv[argv.index("--config") + 1] if "--config" in argv else "relay.config.toml"
    s = settings(conf)
    if not s["enabled"] or not s["owner"]:
        return 0
    try:
        token(s)  # every tick, so a token this job cannot read shows in the log before an outage needs it
    except OSError as e:
        print(f"{time.strftime('%H:%M')} watchdog: cannot read the bridge token: {e}", flush=True)
    what = tick(s)
    if what != "ok":
        print(f"{time.strftime('%H:%M')} watchdog: {what}", flush=True)
    return 0


def demo():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        s = {"state": d, "night": [], "tz": "UTC", "minutes": 3, "text": "down since {since}", "log": d}
        sent = []
        assert tick(s, 1000, None) == "no heartbeat yet"
        fake = lambda s, text: sent.append(text) or "MID1"  # noqa: E731
        hb = os.path.join(d, "heartbeat")
        open(hb, "w").close()
        t0 = os.path.getmtime(hb)
        assert tick(s, t0 + 30, fake) == "ok"
        assert tick(s, t0 + 150, fake) == "stale"          # stale, the count starts
        assert tick(s, t0 + 300, fake) == "stale"          # 2.5 min: not yet
        assert tick(s, t0 + 340, fake) == "alerted" and len(sent) == 1
        assert tick(s, t0 + 400, fake) == "stale" and len(sent) == 1  # once only
        with open(os.path.join(d, "watchdog-down.json")) as f:
            assert json.load(f)["id"] == "MID1"
        os.remove(os.path.join(d, "watchdog-down.json"))
        assert tick(s, t0 + 2000, fake) == "ok"            # a long gap = asleep, count again
        s["night"] = [0, 24]
        for t in (2060, 2120, 2180, 2240, 2300):
            tick(s, t0 + t, fake)
        assert len(sent) == 1, "no alarm at night"
    print("watchdog demo ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        sys.exit(main(sys.argv[1:]))
