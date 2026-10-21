"""Zeus's alert file for Argus (docs/design.md §6; issue #10): ~/.local/state/argus-ui/alerts/zeus.json in the format
every agent uses (argus-ui #8, the same as Hermes's): { "job", "level", "since", "message", "link": "throne" }.
`alert` makes Argus glare and counts in his badge; `warn` only shows on the card. A warning never replaces an alert
that is still up. The file goes away on the next successful deploy, or when the owner marks it seen (`zeus seen`)."""
import json
import os
from pathlib import Path

PATH = "~/.local/state/argus-ui/alerts/zeus.json"      # or $ZEUS_ALERT
LINK = "throne"
LEVELS = ("warn", "alert")


def path():
    return Path(os.path.expanduser(os.environ.get("ZEUS_ALERT") or PATH))


def read():
    """The alert now up, or None (a broken file counts as none)."""
    try:
        doc = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def raise_(job, level, message, now):
    """Write the alert. Returns what is up afterwards (an alert stays when a warning comes in on top of it)."""
    from zeus import queue
    if level not in LEVELS:
        raise ValueError(level)
    up = read()
    if level == "warn" and up and up.get("level") == "alert":
        return up
    doc = {"job": job, "level": level, "since": queue.stamp(now), "message": message, "link": LINK}
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return doc


def clear():
    """Remove the alert; True when there was one."""
    try:
        path().unlink()
        return True
    except FileNotFoundError:
        return False


def main_seen(args):
    """zeus seen: the owner has read the alert; Argus calms on his next refresh."""
    up = read()
    if clear():
        print("zeus seen: cleared the %s \"%s\"" % ((up or {}).get("level", "alert"), (up or {}).get("message", "")))
    else:
        print("zeus seen: no alert was up")
    return 0
