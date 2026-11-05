#!/usr/bin/env python3
"""Give a detached job an owner: its end reaches the coordinator (5 Nov 2026, #134).

Start a new job (preferred; the exit code is the completion event):
  nohup python3 -I scripts/watch_job.py "<#n title>" --run <log file> -- <command...> >/dev/null 2>&1 &
Watch a job that already runs (no exit code; the relay sees the pid is gone):
  python3 -I scripts/watch_job.py "<#n title>" <pid> [<log file>]
Both take --config <relay.config.toml> (default: $RELAY_CONFIG, else ./relay.config.toml).

Writes <state_dir>/jobs/<pid>.json. The relay checks it every minute (job_watch) and sends the coordinator
one '🔔 job' note with the exit code (if known) and the last log lines. --run records the wrapper AND the
command's own process: if the wrapper dies while the command runs, the relay keeps watching the command, and
when it is gone without an exit code the note says "outcome unknown", never "completed".
Wrapper options are read only before "--": the command keeps its own arguments (also its own --config).
"""
import json, os, subprocess, sys, tomllib

CONF = os.environ.get("RELAY_CONFIG", "relay.config.toml")


def jobs_dir(conf):
    conf = os.path.expanduser(conf)
    with open(conf, "rb") as f:
        c = tomllib.load(f)
    here = os.path.dirname(conf)
    return os.path.join(os.path.abspath(os.path.join(here, os.path.expanduser(
        c.get("paths", {}).get("state_dir", "state")))), "jobs")


def proc_start(pid):
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def save(path, rec):
    with open(path + ".tmp", "w") as f:
        json.dump(rec, f)
    os.replace(path + ".tmp", path)  # the relay never reads half a file


def main(argv):
    conf, cmd = CONF, []
    if "--" in argv:  # everything after the first "--" belongs to the command
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 >= len(argv):
            sys.exit(__doc__)
        conf, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    run = len(argv) == 3 and argv[1] == "--run" and cmd
    if not run and (cmd or len(argv) not in (2, 3) or not argv[1].isdigit()):
        sys.exit(__doc__)
    title = argv[0].strip()
    pid = os.getpid() if run else int(argv[1])
    started = proc_start(pid)
    log = os.path.abspath(os.path.expanduser(argv[2])) if len(argv) == 3 else ""

    def refuse(msg):  # under nohup ... >/dev/null stderr is lost: the job's log says it too
        if log:
            with open(log, "a") as f:
                f.write(f"NOT REGISTERED: {msg}\n")
        sys.exit(f"NOT REGISTERED: {msg}")
    if not title or not started:
        refuse("no title" if not title else f"no running process with pid {pid}")
    try:
        d = jobs_dir(conf)
    except (OSError, ValueError) as e:
        refuse(f"relay config {conf}: {e}")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{pid}.json")
    rec = {"title": title, "pid": pid, "started": started, "log": log, "mode": "run" if run else "watch"}
    save(path, rec)
    if not run:
        return print(f"WATCHING pid {pid} ({title}): {path}")
    with open(log, "a") as out:
        try:
            child = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
        except OSError as e:
            out.write(f"watch_job: could not start the command: {e}\n")
            return save(path, {**rec, "exit": 127})
        rec["child"] = {"pid": child.pid, "started": proc_start(child.pid)}
        save(path, rec)  # from here a dead wrapper is not a finished job: the relay watches the child
        code = child.wait()
    save(path, {**rec, "exit": code})  # the completion event: the relay reports it on its next check


if __name__ == "__main__":
    main(sys.argv[1:])
