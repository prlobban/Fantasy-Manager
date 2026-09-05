"""Invoke `claude -p` with the doctrine inlined and the capability surface locked.

Three things make this safe rather than hopeful:

1. **The playbook is inlined verbatim, every run.** Not summarised. A summary
   drifts, and a drifted rule on a live league is a move nobody chose.
2. **`--strict-mcp-config` plus an allowlist.** The model's only tools are
   `core`'s MCP tools, which are the §8.2 write table. No Bash, no web, no
   files — not by instruction, by absence.
3. **The output is validated twice.** `--json-schema` at the CLI, then again
   here, because the shape of a model's reply is never something to trust.

Every transcript is kept under data/agent-runs/ — that is the raw material §7
grades on Tuesday.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.config import REPO_ROOT, settings

log = logging.getLogger(__name__)

PROMPTS = REPO_ROOT / "agent" / "prompts"
SCHEMAS = REPO_ROOT / "agent" / "schemas"
MCP_CONFIG = REPO_ROOT / "agent" / "mcp.json"
PLAYBOOK = REPO_ROOT / "docs" / "fantasy-playbook.md"
DOCTRINE = REPO_ROOT / "docs" / "fantasy-doctrine.md"
PRIORS = REPO_ROOT / "priors.yaml"

#: D8 — the six-part reasoning every manager action must carry. Checked here,
#: in code, so "his projection is higher" cannot become a write.
REASONING_FIELDS = ("reason", "short_term", "long_term", "alternative",
                    "evidence", "would_be_wrong_if")
_MIN_REASON_CHARS = {"reason": 40, "short_term": 20, "long_term": 20,
                     "alternative": 20, "evidence": 20, "would_be_wrong_if": 15}


@dataclass(frozen=True)
class TaskSpec:
    """One agent task and the capability surface it gets.

    Per-task rather than global, because the surfaces genuinely differ: the
    manager tasks need `core`'s write table and the doctrine that governs it,
    a researcher needs the web and must NOT be able to reach a write, and a
    judge needs the doctrine and no tools at all.
    """

    prompt: str
    schema: str
    #: Exact --allowedTools value. "" means no tools at all.
    tools: str
    #: True → launch core's MCP server and allow it. False → no MCP whatsoever.
    mcp: bool
    #: True → system.md + the playbook + priors are prepended. False → the task
    #: brief stands alone. See build_system_prompt.
    doctrine: bool
    max_turns: int | None = None   # None → cfg.agent_max_turns
    #: "research" → cfg.claude_research_model. None → cfg.claude_model.
    model: str | None = None


TASKS: dict[str, TaskSpec] = {
    "daily": TaskSpec("daily.md", "actions.json", "mcp__fantasy__*", True, True),
    "tuesday": TaskSpec("tuesday.md", "review.json", "mcp__fantasy__*", True, True),
    "incoming_trade": TaskSpec(
        "incoming_trade.md", "actions.json", "mcp__fantasy__*", True, True),

    # §3.2 — one research agent per player. The web, and nothing else: with no
    # MCP config it cannot reach set_lineup or any other write, by absence
    # rather than by instruction.
    #
    # WebFetch is withheld deliberately. Every turn of an agentic loop re-sends
    # the whole conversation, so a fetched article (5-15k tokens) is paid for
    # again on every remaining turn. Search snippets answer what this task asks.
    #
    # The playbook is NOT inlined: ~8k tokens x max_turns x every player in the
    # pool exceeds the entire research budget, spent telling a researcher rules
    # it does not apply. This is the difference between the pass fitting a
    # rate-limit window and not.
    # max_turns must be (searches + 2): one turn to issue each search, one to
    # receive the last result, one to write the JSON. Measured 2026-09-04 at 4:
    # the run made 4 searches and died on `max_turns` with the dossier unwritten,
    # having spent the whole $0.36 anyway. A budget that stops one turn short of
    # the answer pays full price for nothing.
    "dossier": TaskSpec(
        "dossier.md", "dossier.json", "WebSearch", False, False,
        max_turns=8, model="research"),

    # D1 / D3.1 — the in-season morning research. Same shape as the pre-draft
    # dossier: the web and nothing else, no doctrine, three searches + the
    # answer. max_turns = searches + 2, measured on the draft pass.
    "weekly_dossier": TaskSpec(
        "weekly_dossier.md", "weekly_dossier.json", "WebSearch", False, False,
        max_turns=7, model="research"),

    # §3.10 — judgment between our turns. Reads a packet, returns JSON. It gets
    # the doctrine (it cites sections) and no tools whatsoever.
    #
    # Two measured corrections, 2026-09-04, both of which cost a full-price
    # run that produced nothing:
    #
    # 1. `--json-schema` delivers the answer THROUGH a tool call named
    #    StructuredOutput. Blanket-denying tools denies the answer: the model
    #    wrote a correct verdict twice and both were refused as permission
    #    denials. So the allowlist names StructuredOutput and nothing else —
    #    no Bash, no web, no MCP, but the one channel it answers on.
    # 2. That tool call costs turns, so max_turns is 3 rather than 1.
    "judge": TaskSpec("judge.md", "verdict.json", "StructuredOutput", False, True,
                      max_turns=3),
}


@dataclass
class AgentResult:
    ok: bool
    task: str
    output: dict | None
    raw: str
    error: str | None = None
    transcript: Path | None = None
    #: Token accounting straight from the CLI envelope — this is what sets the
    #: research budget (§10.3) instead of an estimate.
    usage: dict | None = None


def build_system_prompt(task: str) -> str:
    """The task brief, preceded by the doctrine for tasks that decide.

    Rebuilt on every invocation so the prompt cannot drift from the doctrine on
    disk. If someone edits §4.2, the next run uses the new §4.2.

    Tasks that DECIDE get system.md + the playbook + priors: they cite sections
    and a paraphrase is how a rule quietly changes. Tasks that only RESEARCH
    get neither — system.md tells the reader it manages a live team and that
    its tools are the §8.2 write table, which is false for a researcher and
    exactly the wrong frame. It also costs ~8k tokens per turn per player,
    which is the difference between the pass fitting a rate-limit window
    and not.
    """
    spec = TASKS[task]
    parts: list[str] = []
    if spec.doctrine:
        parts += [
            (PROMPTS / "system.md").read_text(encoding="utf-8"),
            "\n# THE PLAYBOOK (authoritative — cite these sections)\n\n",
            PLAYBOOK.read_text(encoding="utf-8"),
            "\n\n# THE DOCTRINE (the craft — cite D-sections)\n\n",
            DOCTRINE.read_text(encoding="utf-8") if DOCTRINE.exists() else "",
            "\n\n# CURRENT THRESHOLDS (priors.yaml)\n\n```yaml\n",
            PRIORS.read_text(encoding="utf-8"),
            "\n```\n\n# THIS RUN\n\n",
        ]
    parts.append((PROMPTS / spec.prompt).read_text(encoding="utf-8"))
    return "".join(parts)


def _write_mcp_config(path: Path) -> Path:
    """agent/mcp.json, with the MCP server launched by THIS interpreter.

    The checked-in file says `python`, which on the box is not the venv (and
    on Windows is the Store alias). Either way the server would fail to start
    and the model would run with zero tools — no error, just an agent that
    cannot act. So the command is resolved to sys.executable at run time.
    """
    base = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    for server in base.get("mcpServers", {}).values():
        server["command"] = sys.executable
        server.setdefault("env", {})["PYTHONPATH"] = str(REPO_ROOT)
    path.write_text(json.dumps(base, indent=1), encoding="utf-8")
    return path


def _validate(payload: dict, task: str) -> list[str]:
    """Second-pass validation. The CLI already applied the schema; this catches
    the semantic rules a JSON Schema cannot express."""
    problems: list[str] = []

    if task in ("daily", "incoming_trade"):
        actions = payload.get("actions")
        if actions is None:
            return ["missing 'actions'"]
        if not actions and not payload.get("no_action_reason"):
            problems.append("empty actions with no no_action_reason")
        allowed = {"set_lineup", "add_drop", "propose_trade", "reject_trade",
                   "accept_trade", "notify"}
        for i, a in enumerate(actions):
            if a.get("tool") not in allowed:
                problems.append(f"action {i}: unknown tool {a.get('tool')!r}")
            if not a.get("cites"):
                # §8.2a — an uncited action is rejected, not fixed up.
                problems.append(f"action {i}: no § citation")
            # D8 — the reasoning contract. A notify is exempt: it is a message,
            # not a move.
            if a.get("tool") != "notify":
                for f in REASONING_FIELDS:
                    v = a.get(f)
                    if not isinstance(v, str) or len(v.strip()) < _MIN_REASON_CHARS[f]:
                        problems.append(f"action {i}: D8 field {f!r} missing or too thin")
        if task == "daily" and not (payload.get("roster_assessment") or "").strip():
            problems.append("no roster_assessment — the sweep must evaluate the roster every run")

    if task == "tuesday":
        for k in ("result", "efficiency_read", "decision_grades", "lessons"):
            if k not in payload:
                problems.append(f"missing {k!r}")
        for i, ln in enumerate(payload.get("lessons") or []):
            if not isinstance(ln, str) or len(ln.strip()) < 25:
                problems.append(f"lesson {i}: too thin to be a lesson (D7.3)")

    if task == "weekly_dossier":
        if not payload.get("espn_id"):
            problems.append("no espn_id")
        for k, lo, hi in (("week_multiplier", 0.75, 1.25), ("ros_multiplier", 0.85, 1.15)):
            m = payload.get(k)
            if not isinstance(m, int | float) or not (lo <= m <= hi):
                problems.append(f"{k} {m} outside the allowed band")

    if task == "dossier":
        # Only the rules a JSON Schema cannot state. The URL/confidence rules
        # that decide whether a dossier may MOVE the board live in
        # core.draft.dossiers.validate — this layer just refuses a reply that
        # is not a dossier at all.
        if not payload.get("espn_id"):
            problems.append("no espn_id")
        m = payload.get("multiplier")
        if not isinstance(m, int | float) or not (0.85 <= m <= 1.15):
            problems.append(f"multiplier {m} outside the allowed band")
        if payload.get("veto") and not payload.get("veto_reason"):
            problems.append("veto with no veto_reason")

    return problems


def _usage(raw: str) -> dict | None:
    """Token accounting out of the `claude -p --output-format json` envelope.

    §10.3 — the research budget is set from measurement, not estimate, and
    this is where the measurement comes from. Shape-tolerant on purpose: a CLI
    that renames a field should cost us the numbers, never the run.
    """
    try:
        outer = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(outer, dict):
        return None
    u = outer.get("usage")
    out: dict = dict(u) if isinstance(u, dict) else {}
    for k in ("num_turns", "duration_ms", "duration_api_ms", "total_cost_usd"):
        if k in outer:
            out[k] = outer[k]
    return out or None


def run(task: str, packet: dict, *, dry_run: bool = False,
        timeout: int = 600, proc_box: dict | None = None) -> AgentResult:
    """Invoke one agent task.

    `proc_box` is how the judge stays killable: pass a dict and the live
    Popen lands in it as `proc_box["proc"]`, so a watcher thread can end the
    run the instant we are on the clock (§3.10). Without it this is an
    ordinary blocking call.
    """
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}; expected one of {sorted(TASKS)}")

    cfg = settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    run_dir = cfg.agent_runs_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    sys_prompt_path = run_dir / f"{stamp}-{task}-system.md"
    sys_prompt_path.write_text(build_system_prompt(task), encoding="utf-8")

    spec = TASKS[task]
    # --json-schema takes the schema INLINE, not a path. Passing a path made
    # the CLI try to parse "C:\Users\..." as JSON and exit 1 before spending a
    # token — which is how this was found, and why no agent task had ever run.
    schema_json = json.dumps(
        json.loads((SCHEMAS / spec.schema).read_text(encoding="utf-8")),
        separators=(",", ":"),
    )
    user_msg = (
        "Situation packet follows as JSON. Every number you need is in it or "
        "behind a get_* tool. Do not recompute anything.\n\n"
        f"```json\n{json.dumps(packet, indent=1, default=str)}\n```"
    )

    model = cfg.claude_research_model if spec.model == "research" else cfg.claude_model
    # The packet goes in on STDIN, not as an argv. A judge packet carrying 15
    # candidates with their dossiers is ~41k characters, and Windows caps a
    # command line at ~32k: measured 2026-09-04 as
    # "[WinError 206] The filename or extension is too long", with the judge
    # failing instantly on every turn. Linux would have survived this one at
    # ~2MB, but the limit is a cliff nobody sees coming and stdin has none.
    cmd = [
        cfg.claude_bin, "-p",
        "--system-prompt-file", str(sys_prompt_path),
        "--output-format", "json",
        "--json-schema", schema_json,
        "--max-turns", str(spec.max_turns or cfg.agent_max_turns),
        "--model", model,
        "--permission-mode", "bypassPermissions",
    ]

    # --strict-mcp-config on EVERY task, with --mcp-config only where the task
    # is supposed to have tools. Without the strict flag a user-level MCP
    # config on the box would silently hand a researcher the whole write
    # table. With it and no --mcp-config, the task gets zero servers.
    cmd += ["--strict-mcp-config"]
    if spec.mcp:
        cmd += ["--mcp-config", str(_write_mcp_config(run_dir / f"{stamp}-mcp.json"))]
    # An allowlist, always. A blanket --disallowedTools "*" also denies
    # StructuredOutput, which is how --json-schema returns the answer, so a
    # task ends up refusing its own output (measured 2026-09-04).
    if spec.tools:
        cmd += ["--allowedTools", spec.tools]

    if dry_run:
        log.info("DRY RUN — would invoke: %s  <<< %d chars of packet on stdin",
                 " ".join(cmd), len(user_msg))
        return AgentResult(True, task, None, "", error=None, transcript=sys_prompt_path)

    log.info("invoking claude for task %r (%d chars of packet)", task, len(user_msg))
    try:
        with subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            cwd=str(REPO_ROOT),
        ) as proc:
            if proc_box is not None:
                proc_box["proc"] = proc
            try:
                stdout, stderr = proc.communicate(user_msg, timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return AgentResult(False, task, None, "",
                                   error=f"claude timed out after {timeout}s")
    except OSError as e:
        # The box shipped a Windows claude.exe for months; this is what that
        # looks like from here, and it must not read as a model failure.
        return AgentResult(False, task, None, "",
                           error=f"could not run {cfg.claude_bin!r}: {e}")

    if proc.returncode is not None and proc.returncode < 0:
        return AgentResult(False, task, None, stdout or "",
                           error=f"killed (signal {-proc.returncode})")

    raw = stdout or ""
    transcript = run_dir / f"{stamp}-{task}.json"
    transcript.write_text(raw + ("\n--- stderr ---\n" + stderr if stderr else ""),
                          encoding="utf-8")

    # Usage is read before any early return: a run that failed still spent the
    # tokens, and a budget built only from successes understates itself.
    usage = _usage(raw)

    # The support agent learned this the hard way: claude can print a capacity
    # notice and still exit 0. Sniff the output, don't trust the exit code.
    lowered = raw.lower()
    for marker in ("session limit", "usage limit", "rate limit", "credit balance",
                   "overloaded"):
        if marker in lowered:
            return AgentResult(False, task, None, raw,
                               error=f"capacity: {marker}", transcript=transcript,
                               usage=usage)

    if proc.returncode != 0:
        return AgentResult(False, task, None, raw,
                           error=f"claude exited {proc.returncode}: {(stderr or '')[:300]}",
                           transcript=transcript, usage=usage)

    payload = _extract(raw)
    if payload is None:
        return AgentResult(False, task, None, raw,
                           error="could not parse a JSON result from claude's output",
                           transcript=transcript, usage=usage)

    problems = _validate(payload, task)
    if problems:
        return AgentResult(False, task, payload, raw,
                           error="output failed validation: " + "; ".join(problems),
                           transcript=transcript, usage=usage)

    return AgentResult(True, task, payload, raw, transcript=transcript, usage=usage)


def _extract(raw: str) -> dict | None:
    """Pull the structured result out of `claude -p --output-format json`."""
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # The CLI wraps the run; the schema'd answer is usually under "result".
    for key in ("structured_output", "result", "output"):
        v = outer.get(key) if isinstance(outer, dict) else None
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                inner = json.loads(v)
                if isinstance(inner, dict):
                    return inner
            except json.JSONDecodeError:
                continue
    return outer if isinstance(outer, dict) and "actions" in outer else None


def main() -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=sorted(TASKS))
    ap.add_argument("--packet", type=Path, help="JSON file; default builds one live")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.packet:
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
    else:
        from agent.packet import build as build_packet

        packet = build_packet(args.task)

    res = run(args.task, packet, dry_run=args.dry_run)
    print(json.dumps(res.output, indent=1) if res.output else res.raw[:2000])
    if not res.ok:
        print(f"\nFAILED: {res.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
