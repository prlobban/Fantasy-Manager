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
PRIORS = REPO_ROOT / "priors.yaml"

TASKS = {
    "daily": ("daily.md", "actions.json"),
    "predraft": ("predraft.md", "overrides.json"),
    "tuesday": ("tuesday.md", "review.json"),
    "incoming_trade": ("incoming_trade.md", "actions.json"),
}


@dataclass
class AgentResult:
    ok: bool
    task: str
    output: dict | None
    raw: str
    error: str | None = None
    transcript: Path | None = None


def build_system_prompt(task: str) -> str:
    """system.md + the FULL playbook + the current priors + the task brief.

    Rebuilt on every invocation so the prompt cannot drift from the doctrine on
    disk. If someone edits §4.2, the next run uses the new §4.2.
    """
    parts = [
        (PROMPTS / "system.md").read_text(encoding="utf-8"),
        "\n# THE PLAYBOOK (authoritative — cite these sections)\n\n",
        PLAYBOOK.read_text(encoding="utf-8"),
        "\n\n# CURRENT THRESHOLDS (priors.yaml)\n\n```yaml\n",
        PRIORS.read_text(encoding="utf-8"),
        "\n```\n\n# THIS RUN\n\n",
        (PROMPTS / TASKS[task][0]).read_text(encoding="utf-8"),
    ]
    return "".join(parts)


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
        allowed = {"set_lineup", "add_drop", "reject_trade", "notify"}
        for i, a in enumerate(actions):
            if a.get("tool") not in allowed:
                problems.append(f"action {i}: unknown tool {a.get('tool')!r}")
            if not a.get("cites"):
                # §8.2a — an uncited action is rejected, not fixed up.
                problems.append(f"action {i}: no § citation")

    if task == "predraft":
        for i, o in enumerate(payload.get("overrides", [])):
            m = o.get("multiplier")
            if not isinstance(m, int | float) or not (0.85 <= m <= 1.15):
                problems.append(f"override {i}: multiplier {m} outside the allowed band")
            if not o.get("source"):
                problems.append(f"override {i}: no source")

    return problems


def run(task: str, packet: dict, *, dry_run: bool = False,
        timeout: int = 600) -> AgentResult:
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}; expected one of {sorted(TASKS)}")

    cfg = settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    run_dir = cfg.agent_runs_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    sys_prompt_path = run_dir / f"{stamp}-{task}-system.md"
    sys_prompt_path.write_text(build_system_prompt(task), encoding="utf-8")

    schema_path = SCHEMAS / TASKS[task][1]
    user_msg = (
        "Situation packet follows as JSON. Every number you need is in it or "
        "behind a get_* tool. Do not recompute anything.\n\n"
        f"```json\n{json.dumps(packet, indent=1, default=str)}\n```"
    )

    cmd = [
        cfg.claude_bin, "-p", user_msg,
        "--system-prompt-file", str(sys_prompt_path),
        "--output-format", "json",
        "--json-schema", str(schema_path),
        "--mcp-config", str(MCP_CONFIG),
        "--strict-mcp-config",
        "--allowedTools", "mcp__fantasy__*",
        "--max-turns", str(cfg.agent_max_turns),
        "--model", cfg.claude_model,
        "--permission-mode", "bypassPermissions",
    ]

    if dry_run:
        log.info("DRY RUN — would invoke: %s", " ".join(cmd[:2] + ["<packet>"] + cmd[3:]))
        return AgentResult(True, task, None, "", error=None, transcript=sys_prompt_path)

    log.info("invoking claude for task %r (%d chars of packet)", task, len(user_msg))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT)
        )
    except subprocess.TimeoutExpired:
        return AgentResult(False, task, None, "", error=f"claude timed out after {timeout}s")

    raw = proc.stdout or ""
    transcript = run_dir / f"{stamp}-{task}.json"
    transcript.write_text(raw + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""),
                          encoding="utf-8")

    # The support agent learned this the hard way: claude can print a capacity
    # notice and still exit 0. Sniff the output, don't trust the exit code.
    lowered = raw.lower()
    for marker in ("session limit", "usage limit", "rate limit", "credit balance",
                   "overloaded"):
        if marker in lowered:
            return AgentResult(False, task, None, raw,
                               error=f"capacity: {marker}", transcript=transcript)

    if proc.returncode != 0:
        return AgentResult(False, task, None, raw,
                           error=f"claude exited {proc.returncode}: {proc.stderr[:300]}",
                           transcript=transcript)

    payload = _extract(raw)
    if payload is None:
        return AgentResult(False, task, None, raw,
                           error="could not parse a JSON result from claude's output",
                           transcript=transcript)

    problems = _validate(payload, task)
    if problems:
        return AgentResult(False, task, payload, raw,
                           error="output failed validation: " + "; ".join(problems),
                           transcript=transcript)

    return AgentResult(True, task, payload, raw, transcript=transcript)


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
