#!/usr/bin/env python3
"""PreToolUse hook: refuse any Bash `git push` that would update main.

Blocks (exit 2) when a push:
  - names main as a destination (main, HEAD:main, +x:main, :main, refs/heads/main)
  - uses --all or --mirror
  - gives no refspec, or pushes HEAD, while the current branch is main
Commands wrapped in `sh -c`, `bash -c` or `eval` are checked too.
"""
import json
import re
import shlex
import subprocess
import sys

PROTECTED = "main"
SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}
# git global options that take a separate value
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
# git push options that take a separate value
PUSH_OPTS_WITH_VALUE = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}


def current_branch(cwd):
    try:
        out = subprocess.run(
            ["git", "-C", cwd or ".", "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def is_protected(ref):
    ref = ref.lstrip("+")
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/"):]
    return ref == PROTECTED


def check_push(args, cwd):
    """args: tokens after `push`. Returns a reason string if blocked."""
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--all", "--mirror"):
            return f"`git push {a}` would include {PROTECTED}"
        if a in PUSH_OPTS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        positional.append(a)
        i += 1

    refspecs = positional[1:]
    branch = None
    if not refspecs:
        branch = current_branch(cwd)
        if branch == PROTECTED:
            return f"bare `git push` while on {PROTECTED}"
        return None

    for spec in refspecs:
        src, _, dst = spec.partition(":")
        target = dst if dst else src
        if is_protected(target):
            return f"refspec `{spec}` targets {PROTECTED}"
        if not dst and src.lstrip("+") in ("HEAD", "@"):
            if branch is None:
                branch = current_branch(cwd)
            if branch == PROTECTED:
                return f"pushing HEAD while on {PROTECTED}"
    return None


def check_command(command, cwd, depth=0):
    if depth > 3:
        return None
    # Join backslash-continued lines; remaining newlines separate commands.
    command = command.replace("\\\n", " ")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # Unparseable (e.g. unbalanced quotes): fall back to a coarse match.
        if re.search(r"\bgit\b.*\bpush\b", command) and re.search(
            rf"(^|[\s:+/]){PROTECTED}(\s|$)", command
        ):
            return f"could not parse command; it looks like a push to {PROTECTED}"
        return None

    segments, seg = [], []
    for t in tokens:
        if t in SEPARATORS:
            segments.append(seg)
            seg = []
        else:
            seg.append(t)
    segments.append(seg)

    for seg in segments:
        # Recurse into nested shells: sh -c '...', bash -c '...', eval '...'
        for j, t in enumerate(seg):
            if "git" in t and "push" in t and (" " in t or "\n" in t):
                reason = check_command(t, cwd, depth + 1)
                if reason:
                    return reason

        # Find `git [global opts] push ...`
        for j, t in enumerate(seg):
            if t != "git" and not t.endswith("/git"):
                continue
            k = j + 1
            seg_cwd = cwd
            while k < len(seg) and seg[k].startswith("-"):
                if seg[k] == "-C" and k + 1 < len(seg):
                    seg_cwd = seg[k + 1]
                if seg[k] in GIT_OPTS_WITH_VALUE:
                    k += 2
                else:
                    k += 1
            if k < len(seg) and seg[k] == "push":
                reason = check_push(seg[k + 1:], seg_cwd)
                if reason:
                    return reason
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    command = (data.get("tool_input") or {}).get("command", "")
    if "push" not in command:
        return 0
    reason = check_command(command, data.get("cwd"))
    if reason:
        print(
            f"Blocked: {reason}. Never push to {PROTECTED}; push a feature "
            f"branch and open a PR instead.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
