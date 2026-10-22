#!/usr/bin/env python3
"""agent-runner: work GitHub issues with headless Claude Code, one at a time.

The queue is GitHub labels:
  agent:ready    a trusted maintainer approved the issue for the agent
  agent:working  claimed by the runner (set here, when it picks the issue up)
  agent:blocked  the agent needs an answer; its question is an issue comment
  agent:review   the agent opened a PR
An issue is finished, as far as the runner is concerned, once it carries
agent:blocked or agent:review, or is closed. Relabel a blocked issue
agent:ready after answering it and the runner resumes the same Claude session.

Each issue gets its own git worktree and its own Claude session ID, kept in
the state file, so a usage-limit pause, a crash or a reboot resumes exactly
where it stopped. When Claude reports a usage limit, the runner sleeps until
the reset time and resumes.

Commands:
  agent-runner run        loop forever (run it in tmux; see agent-runner.service)
  agent-runner status     show the current task, pause state and queue
  agent-runner pause      finish the current Claude round, then idle
  agent-runner unpause
  agent-runner dry-run    show which issue would be picked next; changes nothing

Config: ~/.config/agent-runner/config.json (see config.example.json).
State and logs: ~/.local/state/agent-runner/.
"""
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

CONFIG_PATH = Path.home() / ".config/agent-runner/config.json"
STATE_DIR = Path.home() / ".local/state/agent-runner"
STATE_PATH = STATE_DIR / "state.json"
PAUSE_PATH = STATE_DIR / "PAUSE"
LOCK_PATH = STATE_DIR / "runner.lock"
LOG_DIR = STATE_DIR / "logs"
PROMPT_PATH = Path(__file__).resolve().parent / "prompt.md"

L_READY, L_WORKING, L_BLOCKED, L_REVIEW = "agent:ready", "agent:working", "agent:blocked", "agent:review"
# Label names a trusted maintainer may have applied to approve an issue
# (ready-for-agent is the pre-rename name of agent:ready).
APPROVAL_LABELS = {L_READY, "ready-for-agent"}

DEFAULTS = {
    "repos": [],                     # [{"path": ..., "github": "owner/name", "link_into_worktree": [...]}]
    "trusted_labelers": [],          # GitHub logins whose agent:ready counts as approval
    "worktree_root": "~/agent-work",
    "max_rounds_per_issue": 12,      # Claude rounds before the runner gives up and blocks the issue
    "max_errors_per_issue": 6,       # consecutive non-limit failures before blocking
    "idle_poll_seconds": 900,        # queue empty or paused
    "between_rounds_seconds": 60,
    "limit_fallback_seconds": 1800,  # usage limit hit but no reset time found
    "limit_margin_seconds": 120,     # wait this long past the reported reset
    "model": None,                   # e.g. "claude-opus-5-5"; None = the CLI default
}

LIMIT_RE = re.compile(r"usage limit|limit reached|hit your (?:usage )?limit|rate.?limit|out of (?:extra )?usage", re.I)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"No config at {CONFIG_PATH}. Copy runner/config.example.json there and edit it.")
    cfg = dict(DEFAULTS)
    cfg.update(json.loads(CONFIG_PATH.read_text()))
    if not cfg["trusted_labelers"]:
        sys.exit("config: trusted_labelers is empty; the runner would trust nobody's approval.")
    for r in cfg["repos"]:
        r["path"] = str(Path(r["path"]).expanduser())
    cfg["worktree_root"] = str(Path(cfg["worktree_root"]).expanduser())
    return cfg


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"current": None, "sessions": {}}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def sh(args, cwd=None, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def gh_json(args, repo):
    return json.loads(sh(["gh", *args, "--repo", repo]) or "null")


def nap(seconds):
    """Sleep in short steps; wake early if the pause flag is set or cleared."""
    paused = PAUSE_PATH.exists()
    end = time.time() + seconds
    while time.time() < end and PAUSE_PATH.exists() == paused:
        time.sleep(min(30, max(0, end - time.time())))


def task_key(task):
    return f"{task['github']}#{task['issue']}"


# ---------------------------------------------------------------------------
# GitHub queue
# ---------------------------------------------------------------------------

def issue_labels(repo, number):
    data = gh_json(["issue", "view", str(number), "--json", "labels,state"], repo)
    return {l["name"] for l in data["labels"]}, data["state"]


def approved_by_trusted(repo, number, trusted):
    """True if the most recent approval-label event came from a trusted login."""
    events = json.loads(sh(["gh", "api", "--paginate", f"repos/{repo}/issues/{number}/events"]) or "[]")
    approvals = [e for e in events
                 if e.get("event") == "labeled" and (e.get("label") or {}).get("name") in APPROVAL_LABELS]
    if not approvals:
        return False
    return (approvals[-1].get("actor") or {}).get("login") in trusted


def list_issues(repo, label):
    return gh_json(["issue", "list", "--label", label, "--state", "open",
                    "--json", "number,title,createdAt", "--limit", "100"], repo)


def pick_next(cfg, state):
    """Next task: an orphaned agent:working issue first, then the oldest trusted agent:ready."""
    for r in cfg["repos"]:
        repo = r["github"]
        for label in (L_WORKING, L_READY):
            for issue in sorted(list_issues(repo, label), key=lambda i: i["createdAt"]):
                n = issue["number"]
                if not approved_by_trusted(repo, n, cfg["trusted_labelers"]):
                    log(f"skip {repo}#{n}: {label} but not approved by {', '.join(cfg['trusted_labelers'])}")
                    continue
                return {"repo_path": r["path"], "github": repo, "issue": n, "title": issue["title"],
                        "link_into_worktree": r.get("link_into_worktree", [])}
    return None


def claim(task):
    repo, n = task["github"], str(task["issue"])
    sh(["gh", "issue", "edit", n, "--repo", repo, "--add-label", L_WORKING, "--remove-label", L_READY])


def give_up(task, reason):
    """Hand the issue back to the maintainer as blocked, with the runner's reason."""
    repo, n = task["github"], str(task["issue"])
    body = (f"**agent-runner:** stopping work on this issue: {reason}\n\n"
            f"Worktree: `{task.get('worktree', '?')}`. Session: `{task.get('session_id', '?')}`. "
            f"Answer here and relabel `{L_READY}` to resume the same session.")
    sh(["gh", "issue", "comment", n, "--repo", repo, "--body", body], check=False)
    sh(["gh", "issue", "edit", n, "--repo", repo, "--add-label", L_BLOCKED, "--remove-label", L_WORKING],
       check=False)


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------

def ensure_worktree(cfg, task):
    """One worktree per issue, detached at the remote default branch; the agent makes its own branch."""
    name = Path(task["repo_path"]).name
    wt = Path(cfg["worktree_root"]) / name / f"issue-{task['issue']}"
    if not wt.exists():
        repo = task["repo_path"]
        sh(["git", "fetch", "-q", "origin"], cwd=repo)
        default = sh(["gh", "repo", "view", task["github"], "--json", "defaultBranchRef",
                      "--jq", ".defaultBranchRef.name"]).strip() or "main"
        wt.parent.mkdir(parents=True, exist_ok=True)
        sh(["git", "worktree", "add", "-q", "--detach", str(wt), f"origin/{default}"], cwd=repo)
        # Gitignored, machine-local files (CLAUDE.md, .claude/) don't come with a
        # worktree; link them in and keep the links out of git.
        exclude = Path(sh(["git", "rev-parse", "--git-common-dir"], cwd=repo).strip())
        exclude = (Path(repo) / exclude if not exclude.is_absolute() else exclude) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text().splitlines() if exclude.exists() else []
        for rel in task["link_into_worktree"]:
            src, dst = Path(repo) / rel, wt / rel
            if src.exists() and not dst.exists():
                dst.symlink_to(src)
            if f"/{rel}" not in existing:
                existing.append(f"/{rel}")
        exclude.write_text("\n".join(existing) + "\n")
        log(f"worktree {wt} at origin/{default}")
    task["worktree"] = str(wt)


def prune_worktrees(cfg):
    """Remove clean worktrees whose issue is closed. Dirty ones are left for a human."""
    for r in cfg["repos"]:
        base = Path(cfg["worktree_root"]) / Path(r["path"]).name
        for wt in sorted(base.glob("issue-*")) if base.exists() else []:
            try:
                _, st = issue_labels(r["github"], int(wt.name.split("-", 1)[1]))
            except Exception:
                continue
            if st == "CLOSED":
                res = subprocess.run(["git", "worktree", "remove", str(wt)], cwd=r["path"],
                                     capture_output=True, text=True)
                log(f"pruned {wt}" if res.returncode == 0 else f"kept {wt}: {res.stderr.strip()}")


# ---------------------------------------------------------------------------
# Claude rounds
# ---------------------------------------------------------------------------

def first_prompt(cfg, task):
    template = PROMPT_PATH.read_text()
    return template.format(repo=task["github"], issue=task["issue"], title=task["title"],
                           trusted=", ".join(cfg["trusted_labelers"]),
                           L_READY=L_READY, L_WORKING=L_WORKING, L_BLOCKED=L_BLOCKED, L_REVIEW=L_REVIEW)


CONTINUE_PROMPT = (
    "Continue working on {repo}#{issue}. You may have been paused by a usage limit or a restart, "
    "so first re-orient: `git status`, `git log --oneline -10`, and the issue's latest comments "
    "(`gh issue view {issue} --comments`). Then carry on. The round ends when you either open the PR "
    "and label the issue {L_REVIEW}, or ask your question and label it {L_BLOCKED}."
)


def run_claude(cfg, task, resume):
    """One Claude round. Returns (outcome, text): outcome is 'ok', 'limit' or 'error'."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"{task['github'].replace('/', '_')}-{task['issue']}.jsonl"
    if resume:
        prompt = CONTINUE_PROMPT.format(repo=task["github"], issue=task["issue"],
                                        L_REVIEW=L_REVIEW, L_BLOCKED=L_BLOCKED)
        session_args = ["--resume", task["session_id"]]
    else:
        prompt = first_prompt(cfg, task)
        session_args = ["--session-id", task["session_id"]]
    cmd = ["claude", "-p", prompt, *session_args, "--permission-mode", "bypassPermissions",
           "--output-format", "stream-json", "--verbose"]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]

    result, tail = None, []
    with open(logfile, "a") as lf:
        lf.write(json.dumps({"runner": "round-start", "at": dt.datetime.now().isoformat(),
                             "resume": resume}) + "\n")
        proc = subprocess.Popen(cmd, cwd=task["worktree"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            lf.write(line)
            lf.flush()
            tail = (tail + [line])[-20:]
            try:
                ev = json.loads(line)
            except ValueError:
                print("  " + line.rstrip()[:200], flush=True)
                continue
            show_event(ev)
            if ev.get("type") == "result":
                result = ev
        proc.wait()

    text = (result or {}).get("result") or "".join(tail)
    if result and not result.get("is_error") and proc.returncode == 0:
        return "ok", text
    if LIMIT_RE.search(text) or LIMIT_RE.search("".join(tail)):
        return "limit", text + "\n" + "".join(tail)
    return "error", text


def show_event(ev):
    """One short line per event, for watching in tmux."""
    t = ev.get("type")
    if t == "assistant":
        for block in (ev.get("message") or {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                print("  > " + block["text"].strip().splitlines()[0][:160], flush=True)
            elif block.get("type") == "tool_use":
                inp = block.get("input") or {}
                detail = inp.get("command") or inp.get("file_path") or inp.get("description") or ""
                print(f"  [{block.get('name')}] {str(detail).splitlines()[0][:140] if detail else ''}", flush=True)
    elif t == "result":
        print(f"  = result: {'error' if ev.get('is_error') else 'ok'} "
              f"({ev.get('num_turns', '?')} turns, ${ev.get('total_cost_usd', 0):.2f} equiv)", flush=True)


def limit_reset_seconds(cfg, text):
    """Seconds to wait for a usage-limit reset, parsed from Claude's message if possible."""
    now = dt.datetime.now().astimezone()
    m = re.search(r"\|(\d{10})\b", text)  # "...usage limit reached|1745000000"
    if m:
        return max(60, int(m.group(1)) - now.timestamp()) + cfg["limit_margin_seconds"]
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?(?:\s*\(([^)]+)\))?", text, re.I)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        tz = now.tzinfo
        if m.group(4) and ZoneInfo:
            try:
                tz = ZoneInfo(m.group(4).strip())
            except Exception:
                pass
        local_now = dt.datetime.now(tz)
        reset = local_now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
        if reset <= local_now - dt.timedelta(minutes=15):
            reset += dt.timedelta(days=1)  # a clock time already well past means tomorrow
        elif reset <= local_now:
            reset = local_now               # just passed: retry after the margin
        return (reset - local_now).total_seconds() + cfg["limit_margin_seconds"]
    return cfg["limit_fallback_seconds"]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def work_round(cfg, state):
    """Advance the current task by one Claude round. Returns seconds to sleep afterwards."""
    task = state["current"]
    labels, st = issue_labels(task["github"], task["issue"])
    if st == "CLOSED" or L_BLOCKED in labels or L_REVIEW in labels:
        log(f"{task_key(task)} is {'closed' if st == 'CLOSED' else 'handed back'}; done")
        state["current"] = None
        save_state(state)
        return 0

    resume = task["rounds"] > 0 or task.get("resumed_session", False)
    log(f"{task_key(task)} round {task['rounds'] + 1} ({'resume' if resume else 'start'}) "
        f"session {task['session_id']}")
    outcome, text = run_claude(cfg, task, resume)

    if outcome == "limit":
        wait = limit_reset_seconds(cfg, text)
        task["resumed_session"] = True  # the session exists now, even if the round died early
        save_state(state)
        until = dt.datetime.now() + dt.timedelta(seconds=wait)
        log(f"usage limit; sleeping until {until:%a %H:%M} ({wait / 60:.0f} min)")
        return wait

    if outcome == "error":
        task["errors"] = task.get("errors", 0) + 1
        task["resumed_session"] = True
        save_state(state)
        log(f"round failed ({task['errors']}/{cfg['max_errors_per_issue']}): {text.strip()[-300:]}")
        if task["errors"] >= cfg["max_errors_per_issue"]:
            give_up(task, f"{task['errors']} consecutive failed rounds. Last output:\n\n```\n{text.strip()[-1500:]}\n```")
            state["current"] = None
            save_state(state)
            return cfg["between_rounds_seconds"]
        return min(3600, 300 * 2 ** (task["errors"] - 1))  # 5, 10, 20, 40, 60 min

    task["rounds"] += 1
    task["errors"] = 0
    save_state(state)
    labels, st = issue_labels(task["github"], task["issue"])
    if st == "CLOSED" or L_BLOCKED in labels or L_REVIEW in labels:
        log(f"{task_key(task)} handed back ({', '.join(sorted(labels & {L_BLOCKED, L_REVIEW})) or 'closed'})")
        state["current"] = None
        save_state(state)
    elif task["rounds"] >= cfg["max_rounds_per_issue"]:
        give_up(task, f"{task['rounds']} Claude rounds without a PR or a question.")
        state["current"] = None
        save_state(state)
    return cfg["between_rounds_seconds"]


def start_task(cfg, state, task):
    key = task_key(task)
    claim(task)
    ensure_worktree(cfg, task)
    prior = state["sessions"].get(key)
    task["session_id"] = prior or str(uuid.uuid4())
    task["resumed_session"] = bool(prior)
    task["rounds"], task["errors"] = 0, 0
    state["sessions"][key] = task["session_id"]
    state["current"] = task
    save_state(state)
    log(f"picked {key}: {task['title']}" + (" (resuming earlier session)" if prior else ""))


def run(cfg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another agent-runner is already running")
    for tool in ("claude", "gh", "git"):
        if not shutil.which(tool):
            sys.exit(f"{tool} is not on PATH")
    log(f"agent-runner started; repos: {', '.join(r['github'] for r in cfg['repos'])}")
    while True:
        try:
            if PAUSE_PATH.exists():
                log(f"paused (remove {PAUSE_PATH} or run `agent-runner unpause`)")
                nap(cfg["idle_poll_seconds"])
                continue
            state = load_state()
            if not state.get("current"):
                prune_worktrees(cfg)
                task = pick_next(cfg, state)
                if not task:
                    log(f"queue empty; next check in {cfg['idle_poll_seconds'] // 60} min")
                    nap(cfg["idle_poll_seconds"])
                    continue
                start_task(cfg, state, task)
            nap(work_round(cfg, state))
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception as e:  # network blips, gh hiccups: be patient
            log(f"runner error: {e}; retrying in 10 min")
            nap(600)


def status(cfg):
    state = load_state()
    print(f"paused: {'yes' if PAUSE_PATH.exists() else 'no'}")
    cur = state.get("current")
    if cur:
        print(f"current: {task_key(cur)} {cur['title']!r} round {cur['rounds']} "
              f"errors {cur.get('errors', 0)}\n  worktree {cur.get('worktree')}\n  session {cur['session_id']}")
    else:
        print("current: none")
    for r in cfg["repos"]:
        for label in (L_WORKING, L_READY, L_BLOCKED, L_REVIEW):
            nums = [f"#{i['number']}" for i in list_issues(r["github"], label)]
            print(f"{r['github']} {label}: {' '.join(nums) or '-'}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "pause":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PAUSE_PATH.touch()
        print("paused; the current Claude round (if any) finishes first")
        return
    if cmd == "unpause":
        PAUSE_PATH.unlink(missing_ok=True)
        print("unpaused; the runner picks up within its poll interval")
        return
    cfg = load_config()
    if cmd == "run":
        run(cfg)
    elif cmd == "status":
        status(cfg)
    elif cmd == "dry-run":
        task = pick_next(cfg, load_state())
        print(f"would pick: {task_key(task)} {task['title']!r}" if task else "would pick: nothing")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
