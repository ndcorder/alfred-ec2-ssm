#!/usr/bin/env python3
"""Render the static icon set into icons/*.png with iconkit.js.

  /usr/bin/python3 -B make_icons.py      (from this folder; exits 1 if any icon failed)
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OK, WARN, BAD, NEUTRAL, INFO = "#15803D", "#B45309", "#B91C1C", "#4B5563", "#1D4ED8"

# name -> (background, SF Symbol). ec2.py refers to these as icons/<name>.png.
JOBS = {
    "running": (OK, "server.rack"),
    "stopped": (NEUTRAL, "stop.fill"),
    "changing": (WARN, "hourglass"),       # pending, stopping, rebooting
    "terminated": (BAD, "xmark"),          # shutting-down, terminated
    "unknown": (NEUTRAL, "questionmark"),  # a state AWS adds later
    "sso": (INFO, "person.badge.key.fill"),
    # Message icons for warnings, errors, hints and empty results.
    "warning": (WARN, "exclamationmark.triangle.fill"),
    "error": (BAD, "xmark.octagon.fill"),
    "hint": (INFO, "lightbulb.fill"),
    "empty": (NEUTRAL, "magnifyingglass"),
}


def main():
    out_dir = os.path.join(HERE, "icons")
    os.makedirs(out_dir, exist_ok=True)
    jobs = [{"out": os.path.join(out_dir, name + ".png"), "bg": bg, "symbol": sym}
            for name, (bg, sym) in sorted(JOBS.items())]
    fd, spec = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(jobs, f)
        p = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", os.path.join(HERE, "iconkit.js"), spec],
                           capture_output=True, text=True, timeout=60)
    finally:
        os.remove(spec)
    sys.stderr.write(p.stderr)
    print("rendered %s" % p.stdout.strip())
    if p.returncode != 0 or p.stdout.strip() != "%d/%d" % (len(jobs), len(jobs)):
        sys.exit(1)


if __name__ == "__main__":
    main()
