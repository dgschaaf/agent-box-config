#!/usr/bin/env python3
"""Add the box's guard-main hook to ~/.claude/settings.json, leaving other settings alone.

Idempotent: any existing guard-main / block-push-to-main entry is replaced, so
rerunning bootstrap.sh never duplicates it. The hook command fails open when
the script is missing, because a PreToolUse hook that exits 2 (as python3 does
on a missing file) would block every Bash call. Prints "changed" or "unchanged".
"""
import json
import shutil
import sys
import time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK = Path.home() / ".claude" / "hooks" / "guard-main.py"
COMMAND = f'f="{HOOK}"; [ -f "$f" ] || exit 0; exec python3 "$f"'
OLD_NAMES = ("guard-main.py", "block-push-to-main.py", "block-main-git.py")


def main():
    try:
        settings = json.loads(SETTINGS.read_text())
    except FileNotFoundError:
        settings = {}
    before = json.dumps(settings, sort_keys=True)

    pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for group in pre:
        group["hooks"] = [h for h in group.get("hooks", [])
                          if not any(n in h.get("command", "") for n in OLD_NAMES)]
    pre[:] = [g for g in pre if g.get("hooks")]
    pre.append({"matcher": "Bash", "hooks": [{"type": "command", "command": COMMAND}]})

    if json.dumps(settings, sort_keys=True) == before:
        print("unchanged")
        return 0
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, f"{SETTINGS}.bak.{time.strftime('%Y%m%d%H%M%S')}")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    print("changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
