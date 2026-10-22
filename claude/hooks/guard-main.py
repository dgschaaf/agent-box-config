#!/usr/bin/env python3
"""PreToolUse hook: refuse Bash git commands that would change main.

Installed box-wide by bootstrap.sh (~/.claude/hooks/guard-main.py), so it
covers every repo Claude works in. GitHub's protect-main ruleset is the real
guardrail; this hook catches mistakes before they leave the box.

Blocks (exit 2) when a command would:
  - push to main: main as a destination (main, HEAD:main, +x:main, :main,
    refs/heads/main), --all or --mirror, or a bare push / push of HEAD while
    the current branch is main
  - add history to main locally: commit, merge, rebase, cherry-pick, revert or
    am while the current branch is main
  - delete, rename or force-move main: branch -d/-D/-m/-M/-f, checkout -B,
    switch -C, update-ref
Read-only and syncing commands (status, log, diff, fetch, pull, checkout main)
are allowed. A `git checkout main` or `cd` earlier in the same command is
followed, so `git checkout main && git merge x` is caught. Commands wrapped in
`sh -c`, `bash -c` or `eval` are checked too.
"""
import json
import os
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
# subcommands that add commits to the current branch
WRITES_CURRENT_BRANCH = {"commit", "merge", "rebase", "cherry-pick", "revert", "am"}
BRANCH_CHANGING_OPTS = {"-d", "-D", "--delete", "-m", "-M", "--move", "-f", "--force",
                        "-c", "-C", "--copy"}


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


def check_push(args, branch_of):
    """args: tokens after `push`; branch_of(): current branch. Returns a reason if blocked."""
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
    if not refspecs:
        if branch_of() == PROTECTED:
            return f"bare `git push` while on {PROTECTED}"
        return None

    for spec in refspecs:
        src, _, dst = spec.partition(":")
        target = dst if dst else src
        if is_protected(target):
            return f"refspec `{spec}` targets {PROTECTED}"
        if not dst and src.lstrip("+") in ("HEAD", "@") and branch_of() == PROTECTED:
            return f"pushing HEAD while on {PROTECTED}"
    return None


def check_local(sub, args, branch_of):
    """Non-push subcommands. Returns (reason, branch after the command or None)."""
    positional = [a for a in args if not a.startswith("-")]
    if sub in ("checkout", "switch"):
        for flag in ("-b", "-c", "-B", "-C", "--orphan", "--force-create", "--create"):
            if flag in args:
                i = args.index(flag)
                new = args[i + 1] if i + 1 < len(args) else ""
                if flag in ("-B", "-C", "--force-create") and is_protected(new):
                    return f"`git {sub} {flag} {new}` would reset {PROTECTED}", None
                return None, new or None
        if positional and "--" not in args and is_protected(positional[0]):
            return None, PROTECTED
        return None, None
    if sub == "branch":
        if any(a in BRANCH_CHANGING_OPTS for a in args) and any(is_protected(p) for p in positional):
            return f"`git branch` would delete, move or overwrite {PROTECTED}", None
        return None, None
    if sub == "update-ref" and any(is_protected(p) for p in positional):
        return f"`git update-ref` on {PROTECTED}", None
    if sub in WRITES_CURRENT_BRANCH and branch_of() == PROTECTED:
        return f"`git {sub}` while on {PROTECTED}", None
    return None, None


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
# A heredoc on a line that runs one of these is code, not data.
SHELL_WORD_RE = re.compile(r"(^|[\s;&|(/])(bash|sh|dash|zsh|ksh|eval|source|python3?)(\s|$)")


def strip_heredocs(command):
    """Drop heredoc bodies that are data (commit messages, file contents), whose
    apostrophes would otherwise break shlex. Bodies fed to a shell (bash <<EOF,
    cat <<EOF | sh) are kept as commands, so a push inside one is still checked."""
    out, lines, i = [], command.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        bare = HEREDOC_RE.sub("", line)
        out.append(bare)
        is_code = bool(SHELL_WORD_RE.search(bare))
        for m in HEREDOC_RE.finditer(line):
            i += 1
            while i < len(lines) and lines[i].strip() != m.group(2):
                if is_code:
                    out.append(lines[i])
                i += 1
        i += 1
    return "\n".join(out)


def check_command(command, cwd, depth=0):
    if depth > 3:
        return None
    command = strip_heredocs(command)
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

    # Branch as changed by earlier segments (e.g. `git checkout main && ...`);
    # None means ask git.
    assumed = None
    for seg in segments:
        # Recurse into nested shells: sh -c '...', bash -c '...', eval '...'
        for t in seg:
            if "git" in t and (" " in t or "\n" in t):
                reason = check_command(t, cwd, depth + 1)
                if reason:
                    return reason

        if len(seg) >= 2 and seg[0] == "cd":
            cwd = os.path.join(cwd or ".", os.path.expanduser(seg[1]))
            assumed = None
            continue

        # Find `git [global opts] <subcommand> ...`
        for j, t in enumerate(seg):
            if t != "git" and not t.endswith("/git"):
                continue
            k = j + 1
            seg_cwd = cwd
            while k < len(seg) and seg[k].startswith("-"):
                if seg[k] == "-C" and k + 1 < len(seg):
                    seg_cwd = os.path.join(cwd or ".", seg[k + 1])
                if seg[k] in GIT_OPTS_WITH_VALUE:
                    k += 2
                else:
                    k += 1
            if k >= len(seg):
                continue
            sub, args = seg[k], seg[k + 1:]
            branch_of = (lambda c=seg_cwd, a=assumed: a if a is not None else current_branch(c))
            if sub == "push":
                reason = check_push(args, branch_of)
            else:
                reason, new_branch = check_local(sub, args, branch_of)
                if new_branch is not None:
                    assumed = new_branch
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
    if "git" not in command:
        return 0
    reason = check_command(command, data.get("cwd"))
    if reason:
        print(
            f"Blocked by ~/.claude/hooks/guard-main.py: {reason}. {PROTECTED} only "
            f"changes through a reviewed PR; work on a feature branch and open a PR.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
