#!/usr/bin/env python3
"""Tests for claude/hooks/guard-main.py.  Run: python3 tests/test_guard_main.py

Each case feeds the hook a PreToolUse payload from a scratch repo checked out
on either main or a feature branch, and checks the exit code (2 = blocked).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "claude", "hooks", "guard-main.py")

# (expected exit, command), run from a repo on a feature branch
ON_FEATURE = [
    (2, "git push origin main"),
    (2, "git push -u origin main"),
    (2, "git push --force origin HEAD:main"),
    (2, "git push origin +feature:refs/heads/main"),
    (2, "git push origin :main"),
    (2, "git push --all origin"),
    (2, "cd x && git push origin main"),
    (2, "git -C /tmp push origin main"),
    (2, "bash -c 'git push origin main'"),
    (2, "git push origin feature main"),
    (2, "git \\\n  push origin \\\n  main"),
    (2, "echo hi\ngit push origin main"),
    (2, "git checkout main && git merge feature"),
    (2, "git switch main; git commit -m x"),
    (2, "git branch -D main"),
    (2, "git branch -f main HEAD"),
    (2, "git checkout -B main"),
    (2, "git update-ref refs/heads/main HEAD"),
    (0, "git push origin bootstrap-script"),
    (0, "git push -u origin fix-main-bug"),
    (0, "git push origin main-docs"),
    (0, "git push"),
    (0, "git push origin HEAD"),
    (0, "git log main"),
    (0, "echo push main"),
    (0, "git pull origin main"),
    (0, "git commit -m 'fix main loop'"),
    (0, "git merge origin/main"),
    (0, "git rebase main"),
    (0, "git checkout main"),
    (0, "git checkout -b agent/7-x origin/main"),
    (0, "git checkout main && git checkout -b agent/7-x && git commit -m x"),
    (0, "git commit -F - <<'EOF'\nblocks any git push\nthat would update main:\nEOF\ngit push origin bootstrap-script"),
    (0, "git push origin bootstrap-script\necho main"),
    (0, "git commit -q -F - <<'EOF'\nDarren's main point: pio wasn't installed\nEOF\ngit push -u origin agent-runner"),
    (0, "cat > f <<EOF\ngit push origin main\nEOF"),
    (2, "git commit -F - <<'EOF'\nit's done\nEOF\ngit push origin main"),
]

# run from a repo on main
ON_MAIN = [
    (2, "git push"),
    (2, "git push origin HEAD"),
    (2, "git commit -m x"),
    (2, "git merge feature"),
    (2, "git rebase origin/main"),
    (2, "git cherry-pick abc123"),
    (2, "git revert HEAD"),
    (0, "git status"),
    (0, "git pull"),
    (0, "git fetch origin"),
    (0, "git reset --hard origin/main"),
    (0, "git checkout -b agent/7-x && git commit -m x"),
    (0, "git push origin agent/7-x"),
]


def run_hook(command, cwd):
    payload = {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True)
    return r.returncode


def make_repo(branch):
    d = tempfile.mkdtemp(prefix="guard-main-")
    subprocess.run(["git", "init", "-q", "-b", branch, d], check=True)
    return d


class GuardMainTest(unittest.TestCase):
    def check(self, cases, branch):
        repo = make_repo(branch)
        for expected, command in cases:
            with self.subTest(branch=branch, command=command):
                self.assertEqual(run_hook(command, repo), expected)

    def test_on_feature_branch(self):
        self.check(ON_FEATURE, "feature")

    def test_on_main(self):
        self.check(ON_MAIN, "main")

    def test_non_bash_tool_ignored(self):
        payload = {"tool_name": "Edit", "tool_input": {"command": "git push origin main"}}
        r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
