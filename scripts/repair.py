#!/usr/bin/env python
"""Self-repair: turn what a run reported as wrong into a tested, deployed fix.

Pearce, 2026-09-27: "expand the self healing to be all encompassing" — and
auto-deploy, tell him after. Until now self-healing meant one thing (re-pointing
a stale browser selector, core/browser/selfheal.py). Every other fault — a
lineup write that benched Jefferson, a note that listed kickers as defences —
was diagnosed perfectly by the manager, posted as "Needs Pearce", and then sat
there until a human read it. The D/ST note sat for four runs.

The loop, run by cron_manage.sh after EVERY task:

  0. rollback  — if the last deployed repair is unproven and this run crashed,
                 revert it and say so. If this run was clean, mark it proven.
  1. collect   — the run's escalation, failed writes, tracebacks (issues.py).
                 Operational faults (auth, cookies, capacity) are skipped: no
                 diff fixes them and they already alert.
  2. repair    — `claude -p` in a throwaway git worktree, SANDBOXED: dummy ESPN
                 and Slack credentials, the write switch off, an allowlist of
                 tools (read/edit, pytest, ruff, read-only git). It cannot reach
                 the league. It must write a failing regression test first.
  3. guard     — core/repair/guard.py, in code: protected paths, tests only
                 grow, the MCP write table unchanged, the new test fails on the
                 old code, full suite + ruff green, size cap.
  4. deploy    — fast-forward the live checkout under the run lock, so a fix
                 never lands mid-sweep. Post to #fantasy.

Caps: REPAIR_MAX_PER_DAY runs a day (default 3), REPAIR_MAX_USD per run
(default 6), two tries per fault per week. After that it is Pearce's.

Usage (cron): repair.py --task sweep --rc 0 --log-offset 12345 --since <iso>
Manual:       repair.py --issue "describe the fault" [--no-deploy]
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent import run as agent_run  # noqa: E402
from core.config import settings  # noqa: E402
from core.notify import notify  # noqa: E402
from core.repair import guard  # noqa: E402
from core.repair import issues as I  # noqa: E402

log = logging.getLogger("repair")

CFG = settings()
DATA = CFG.data_dir
REPAIR_DIR = DATA / "repair"
LEDGER = REPAIR_DIR / "ledger.jsonl"
LOCK = DATA / "run.lock"
MANAGER_LOG = DATA / "manager.log"
JOURNAL = DATA / "journal.jsonl"
PY = sys.executable

MAX_PER_DAY = int(os.environ.get("REPAIR_MAX_PER_DAY", "3"))
MAX_USD = os.environ.get("REPAIR_MAX_USD", "6")
MODEL = os.environ.get("REPAIR_MODEL", "") or CFG.claude_model
TIMEOUT_S = 45 * 60

_SANDBOX_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "TZ",
                 "CLAUDE_CODE_OAUTH_TOKEN")


# ── plumbing ─────────────────────────────────────────────────────────────────


def git(*args: str, cwd: Path = REPO, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8")
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def git_as_polaris(*args: str, cwd: Path = REPO) -> str:
    return git("-c", "user.name=Polaris (self-repair)",
               "-c", "user.email=polaris-repair@fantasy-manager.local", *args, cwd=cwd)


@contextlib.contextmanager
def run_lock(wait_s: int = 1800):
    """The same lock cron_manage.sh holds while a task runs: a deploy or a
    revert never swaps code out from under a live sweep."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a") as fh:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("run lock held too long") from None
                time.sleep(5)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def sandbox_env(tree: Path) -> dict[str, str]:
    """An environment with no real credential in it. Built from an allowlist,
    not by deleting keys: settings() has already loaded the live .env into
    os.environ, and a denylist forgets the one key that mattered."""
    sb = tree / ".sandbox"
    sb.mkdir(exist_ok=True)
    (sb / "ENABLED").write_text("off\n")
    base = agent_run._child_env()
    env = {k: base[k] for k in _SANDBOX_KEEP if k in base}
    env["PATH"] = f"{Path(PY).parent}:{env.get('PATH', '/usr/bin:/bin')}"
    env.update(
        ESPN_SWID="{00000000-0000-0000-0000-000000000000}", ESPN_S2="sandbox",
        ESPN_LEAGUE_ID="1", ESPN_SEASON=str(CFG.season), ESPN_TEAM_NAME="sandbox",
        DATA_DIR=str(sb), ENABLED_FILE=str(sb / "ENABLED"), CLAUDE_BIN="/bin/false",
        SLACK_CHANNEL_ID="C0SANDBOX", SLACK_TOKEN_FILE=str(sb / "no-token"),
    )
    return env


def _exclude_scratch() -> None:
    """Keep the worktree's scratch dirs out of every diff and commit."""
    common = Path(git("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = REPO / common
    ex = common / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    have = ex.read_text() if ex.exists() else ""
    add = [p for p in (".sandbox/", ".repair/") if p not in have.splitlines()]
    if add:
        ex.write_text(have.rstrip("\n") + "\n" + "\n".join(add) + "\n")


def _drop_worktree(path: Path) -> None:
    with contextlib.suppress(Exception):
        git("worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
    git("worktree", "prune", check=False)


def _tell(level: str, title: str, body: str) -> None:
    log.info("%s | %s | %s", level, title, body)
    notify(level, title, body)


# ── 0. rollback ──────────────────────────────────────────────────────────────


def settle_last_deploy(ledger: list[dict], task: str, rc: int, log_text: str) -> bool:
    """Revert an unproven repair the moment a run crashes under it; prove it
    after a clean run. Returns True if it reverted (skip repairing this run:
    the crash is the revert's evidence, not a new fault)."""
    pend = I.pending_deploy(ledger)
    if not pend:
        return False
    crashed = (rc != 0 or "Traceback (most recent call last)" in log_text) \
        and not I.NOT_CODE.search(log_text[-4000:])
    if not crashed:
        if task in ("sweep", "lineup", "tuesday"):
            I.append_ledger(LEDGER, I.LedgerEntry(
                at=datetime.now(UTC).isoformat(), status="proven",
                commit=pend.get("commit"), summary=f"clean {task} run after deploy"))
        return False

    commit = pend.get("commit")
    why = (I._last_traceback(log_text) or log_text[-1500:])[-1500:]
    try:
        with run_lock():
            git_as_polaris("revert", "--no-edit", commit)
        status, detail = "reverted", why
        _tell("warn", "Self-repair rolled back",
              f"The run after repair `{commit[:7]}` crashed, so it was reverted "
              f"(`{git('rev-parse', '--short', 'HEAD')}`). Next repair attempt sees why.\n"
              f"```{why[-600:]}```")
    except Exception as e:
        with contextlib.suppress(Exception):
            git("revert", "--abort", check=False)
        status, detail = "error", f"revert failed: {e}"
        _tell("error", "Self-repair could not roll back",
              f"Repair `{commit[:7]}` preceded a crash and `git revert` failed: {e}. "
              "The box is running it. Needs a human.")
    I.append_ledger(LEDGER, I.LedgerEntry(
        at=datetime.now(UTC).isoformat(), status=status, commit=commit,
        issues=pend.get("issues", []), detail=detail))
    return True


# ── 2. the repair agent ──────────────────────────────────────────────────────


def _evidence(wt: Path, issues: list[I.Issue], log_text: str, since: datetime) -> None:
    ev = wt / ".repair"
    ev.mkdir(exist_ok=True)
    (ev / "issues.json").write_text(json.dumps(
        [{"key": i.key, "source": i.source, "text": i.text, "evidence": i.evidence}
         for i in issues], indent=1))
    (ev / "run-log.txt").write_text(log_text[-30000:])
    with contextlib.suppress(OSError):
        (ev / "journal-last.json").write_text(JOURNAL.read_text().splitlines()[-1])
    with contextlib.suppress(OSError):
        shutil.copy(LEDGER, ev / "ledger.jsonl")
    reasoning = sorted((DATA / "reasoning").glob("*.md"), key=lambda p: p.stat().st_mtime)
    if reasoning:
        shutil.copy(reasoning[-1], ev / f"reasoning-{reasoning[-1].name}")
    cutoff = since.timestamp() - 60
    shots = [p for p in (DATA / "screenshots").glob("*.png") if p.stat().st_mtime >= cutoff]
    for p in sorted(shots)[-6:]:
        shutil.copy(p, ev / p.name)
    (ev / "commands.txt").write_text(
        f"tests:  {PY} -m pytest -q -p no:cacheprovider [node ids]\n"
        f"lint:   {PY} -m ruff check .\n")


def call_agent(wt: Path, env: dict[str, str],
               issues: list[I.Issue]) -> tuple[dict | None, str, float | None]:
    schema = json.dumps(json.loads((REPO / "agent/schemas/repair.json").read_text()),
                        separators=(",", ":"))
    allowed = ",".join([
        "Read", "Edit", "Write", "Glob", "Grep", "StructuredOutput",
        f"Bash({PY} -m pytest:*)", f"Bash({PY} -m ruff:*)",
        "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)", "Bash(git show:*)",
    ])
    denied = ",".join([
        "WebFetch", "WebSearch", "Bash(git commit:*)", "Bash(git push:*)",
        f"Read(/{REPO}/.env)", f"Read(/{DATA}/**)",
    ])
    cmd = [
        CFG.claude_bin, "-p",
        "--system-prompt-file", str(REPO / "agent/prompts/repair.md"),
        "--output-format", "json", "--json-schema", schema,
        "--max-turns", "150", "--model", MODEL,
        "--permission-mode", "dontAsk", "--strict-mcp-config",
        "--max-budget-usd", MAX_USD,
        "--allowedTools", allowed, "--disallowedTools", denied,
    ]
    msg = ("Repair the issues below. Evidence is in .repair/. Return one entry per "
           "issue key.\n\n```json\n"
           + json.dumps([{"key": i.key, "source": i.source, "text": i.text} for i in issues],
                        indent=1) + "\n```")
    try:
        p = subprocess.run(cmd, input=msg, cwd=str(wt), env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=TIMEOUT_S)
        raw = p.stdout or ""
    except subprocess.TimeoutExpired:
        return None, f"repair agent timed out after {TIMEOUT_S}s", None
    (REPAIR_DIR / f"{wt.name}-transcript.json").write_text(raw + "\n--- stderr ---\n" + (p.stderr or ""))
    usage = agent_run._usage(raw) or {}
    cost = usage.get("total_cost_usd")
    payload = agent_run._extract(raw)
    if payload is None:
        return None, agent_run._cli_message(raw) or f"no structured result (exit {p.returncode})", cost
    return payload, "", cost


# ── 1–4. one repair attempt ──────────────────────────────────────────────────


def attempt(issues: list[I.Issue], log_text: str, since: datetime, *, deploy: bool) -> int:
    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    base = git("rev-parse", "HEAD")
    wt = REPAIR_DIR / f"wt-{stamp}"
    base_wt = REPAIR_DIR / f"base-{stamp}"
    branch = f"repair/{stamp}"
    REPAIR_DIR.mkdir(parents=True, exist_ok=True)
    _exclude_scratch()
    git("worktree", "add", "-q", "-b", branch, str(wt), base)
    entry = I.LedgerEntry(at=now.isoformat(), status="error", base=base,
                          issues=[{"key": i.key, "source": i.source, "text": i.text[:500]}
                                  for i in issues])
    try:
        env = sandbox_env(wt)
        _evidence(wt, issues, log_text, since)
        payload, err, entry.cost_usd = call_agent(wt, env, issues)
        if payload is None:
            entry.detail = err
            if not I.NOT_CODE.search(err):
                _tell("warn", "Self-repair: agent failed", err[:400])
            return 1

        entry.summary = payload.get("summary", "")
        results = payload.get("issues", [])
        for r in results:
            for i in entry.issues:
                if i["key"] == r.get("key"):
                    i.update(status=r.get("status"), root_cause=r.get("root_cause", "")[:600],
                             fix=r.get("fix", "")[:600])
        fixed = [r for r in results if r.get("status") == "fixed"]
        lines = [f"• *{r.get('status')}* — {r.get('fix', '')[:300]}" for r in results]

        git("add", "-A", cwd=wt)
        diff = git("diff", "--cached", base, cwd=wt)
        if not fixed or not diff.strip():
            entry.status = "no_fix"
            _tell("info", "Self-repair: no code change", entry.summary + "\n" + "\n".join(lines))
            return 0

        git("worktree", "add", "-q", "--detach", str(base_wt), base)
        v = guard.verify_tree(wt, base_wt, PY, env, diff)
        if not v.ok:
            entry.status, entry.detail = "rejected", "\n".join(v.problems)[:3000]
            _tell("warn", "Self-repair held back its own fix",
                  f"{entry.summary}\nThe guard refused it:\n"
                  + "\n".join(f"• {p[:300]}" for p in v.problems[:5])
                  + f"\nBranch `{branch}` kept on the box.")
            return 1

        body = entry.summary + "\n\n" + "\n".join(
            f"root cause: {r.get('root_cause', '')}\nfix: {r.get('fix', '')}" for r in fixed)
        git_as_polaris("commit", "-q", "-m", f"repair: {entry.summary[:68]}\n\n{body}", cwd=wt)
        commit = git("rev-parse", "HEAD", cwd=wt)
        entry.commit = commit
        if not deploy:
            entry.status = "no_fix"
            entry.detail = f"--no-deploy: passed the guard as {commit[:7]} on {branch}"
            _tell("info", "Self-repair: fix ready, not deployed (--no-deploy)", body[:1500])
            return 0

        with run_lock():
            if git("rev-parse", "HEAD") != base:
                raise RuntimeError("live checkout moved during the repair — not deploying onto it")
            if git("status", "--porcelain", "--untracked-files=no"):
                raise RuntimeError("live checkout has uncommitted changes — not deploying onto it")
            git("merge", "--ff-only", "-q", branch)
        entry.status = "deployed"
        _tell("info", f"🔧 Self-repair deployed `{commit[:7]}`",
              f"{entry.summary}\n" + "\n".join(lines)
              + f"\n{len(v.new_tests)} regression test(s) added, suite green. "
                f"Auto-reverts if the next run crashes. Undo: `git revert {commit[:7]}`")
        return 0
    except Exception as e:
        entry.detail = f"{type(e).__name__}: {e}"
        _tell("error", "Self-repair errored", entry.detail[:500])
        return 1
    finally:
        I.append_ledger(LEDGER, entry)
        _drop_worktree(wt)
        _drop_worktree(base_wt)
        # Keep a branch only when it holds a fix a human may want to read:
        # one the guard refused, or one that passed but was not deployed.
        if entry.status == "deployed":
            git("branch", "-d", branch, check=False)
        elif entry.status != "rejected" and not entry.commit:
            git("branch", "-D", branch, check=False)


# ── entry ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="manual")
    ap.add_argument("--rc", type=int, default=0)
    ap.add_argument("--log-offset", type=int, default=None)
    ap.add_argument("--since", default=None)
    ap.add_argument("--issue", action="append", default=[],
                    help="describe a fault by hand (repeatable)")
    ap.add_argument("--no-deploy", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    since = (datetime.fromisoformat(args.since) if args.since
             else datetime.now(UTC) - timedelta(minutes=90))
    offset = args.log_offset if args.log_offset is not None else max(
        0, (MANAGER_LOG.stat().st_size if MANAGER_LOG.exists() else 0) - 200_000)
    log_text = I.log_slice(MANAGER_LOG, offset)
    ledger = I.read_ledger(LEDGER)

    if not args.issue and settle_last_deploy(ledger, args.task, args.rc, log_text):
        return 0

    found = [I.Issue("manual", t) for t in args.issue] or \
        I.collect(JOURNAL, log_text, since=since)
    todo = found if args.issue else I.fresh(found, ledger)
    if not todo:
        log.info("nothing to repair (%d found, all tried or none)", len(found))
        return 0
    if I.attempts_today(ledger) >= MAX_PER_DAY and not args.issue:
        log.info("daily cap reached (%d) — %d issue(s) wait", MAX_PER_DAY, len(todo))
        return 0
    log.info("repairing %d issue(s): %s", len(todo), [i.text[:80] for i in todo])
    return attempt(todo, log_text, since, deploy=not args.no_deploy)


if __name__ == "__main__":
    sys.exit(main())
