"""What an unattended fix must prove before it ships. All of it in code.

Pearce, 2026-09-27: expand self-healing to be all-encompassing, auto-deploy,
tell me after. The repair agent is a model editing the code that writes to a
live money league with nobody watching, so "the tests passed" is not enough —
the model writes the tests. Each check below closes a specific way a
plausible-looking fix does damage:

  1. **Protected paths.** The gates, the kill switch, the health check, the
     gauntlet, the doctrine, the priors, the prompts and schemas, the deploy
     plumbing and this file are never touched. A repair that needs one of them
     is a decision for Pearce, not a fix. (An agent that can edit its own
     guard has no guard.)
  2. **Tests only grow.** No line of an existing test file is removed. The
     cheapest way to make a suite pass is to weaken it.
  3. **The write surface is unchanged.** The set of MCP tools IS the write
     table (§8.2). A repair may change how a tool works, never which tools
     exist.
  4. **A regression test that fails on the old code.** A fix with no test that
     reproduces the fault has not shown it understood the fault.
  5. **Full suite + lint green** on the fixed tree.
  6. **Size cap.** A 400-line "fix" is a redesign.

Everything here is pure (diff text in, verdict out) except the runners at the
bottom, so the rules themselves are unit-tested.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PROTECTED: tuple[str, ...] = (
    "core/gates/*",
    "core/espn/health.py",
    "core/manager/gauntlet.py",
    "core/repair/*",
    "core/config.py",
    "scripts/repair.py",
    "scripts/cron_manage.sh",
    "scripts/setup_box.sh",
    "scripts/healthcheck.py",
    "agent/prompts/*",
    "agent/schemas/*",
    "agent/run.py",
    "docs/fantasy-playbook.md",
    "docs/fantasy-doctrine.md",
    "priors.yaml",
    "pyproject.toml",
    ".gitignore",
    ".gitattributes",
    "ENABLED",
    ".env*",
    "tests/test_write_gate.py",
    "tests/test_repair_guard.py",
)

MAX_CHANGED_LINES = 400  # non-test lines added + removed


@dataclass
class Verdict:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    new_tests: list[str] = field(default_factory=list)  # pytest node ids

    def fail(self, why: str) -> None:
        self.ok = False
        self.problems.append(why)


def is_protected(path: str) -> bool:
    # removeprefix, not lstrip("./"): lstrip strips CHARACTERS, and turned
    # ".env" into "env" — unprotected. Caught by this module's own test.
    p = path.replace("\\", "/").removeprefix("./")
    return any(fnmatch.fnmatch(p, pat) for pat in PROTECTED)


# ── diff parsing ─────────────────────────────────────────────────────────────


@dataclass
class FileDiff:
    path: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    deleted_file: bool = False


def parse_diff(diff: str) -> list[FileDiff]:
    """A unified `git diff` split per file into added and removed lines."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur = FileDiff(path=m.group(2) if m else line)
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("deleted file mode"):
            cur.deleted_file = True
        elif line.startswith(("+++", "---")):
            continue
        elif line.startswith("+"):
            cur.added.append(line[1:])
        elif line.startswith("-"):
            cur.removed.append(line[1:])
    return files


def _is_test(path: str) -> bool:
    return path.startswith("tests/")


_TEST_DEF = re.compile(r"^def (test_\w+)\s*\(")


def check_diff(diff: str) -> Verdict:
    """Checks 1, 2, 4 (a new test exists) and 6, on the diff alone."""
    v = Verdict()
    files = parse_diff(diff)
    if not files:
        v.fail("empty diff — nothing was changed")
        return v

    changed = 0
    for f in files:
        if is_protected(f.path):
            v.fail(f"touches protected path {f.path}")
        if _is_test(f.path):
            if f.deleted_file:
                v.fail(f"deletes test file {f.path}")
            elif any(line.strip() for line in f.removed):
                v.fail(f"removes lines from existing test file {f.path} — tests only grow")
            for line in f.added:
                if m := _TEST_DEF.match(line):
                    v.new_tests.append(f"{f.path}::{m.group(1)}")
        else:
            changed += len(f.added) + len(f.removed)

    if changed > MAX_CHANGED_LINES:
        v.fail(f"{changed} non-test lines changed (cap {MAX_CHANGED_LINES}) — a redesign, not a fix")
    if not v.new_tests:
        v.fail("no new regression test (a `def test_...` added under tests/)")
    return v


def tool_surface(source: str) -> set[str]:
    """Names of every `@mcp.tool()` function in core/mcp_server.py — the write
    table (§8.2) as code. Read by AST, not import, so a broken module cannot
    hide a changed surface behind an ImportError."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for d in node.decorator_list:
                target = d.func if isinstance(d, ast.Call) else d
                if (isinstance(target, ast.Attribute) and target.attr == "tool"
                        and isinstance(target.value, ast.Name) and target.value.id == "mcp"):
                    names.add(node.name)
    return names


def check_surface(base_src: str, fixed_src: str) -> list[str]:
    try:
        before, after = tool_surface(base_src), tool_surface(fixed_src)
    except SyntaxError as e:
        return [f"core/mcp_server.py does not parse: {e}"]
    probs = []
    if added := after - before:
        probs.append(f"adds MCP tools {sorted(added)} — the write table is Pearce's")
    if gone := before - after:
        probs.append(f"removes MCP tools {sorted(gone)} — the write table is Pearce's")
    return probs


# ── runners (subprocess; not unit-tested) ────────────────────────────────────


def run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int = 1200
        ) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def verify_tree(fixed: Path, base: Path, python: str, env: dict[str, str],
                diff: str) -> Verdict:
    """The whole gate: diff rules, surface, fails-on-base, suite, lint."""
    v = check_diff(diff)
    if not v.ok:
        return v

    for p in check_surface((base / "core/mcp_server.py").read_text(encoding="utf-8"),
                           (fixed / "core/mcp_server.py").read_text(encoding="utf-8")):
        v.fail(p)
    if not v.ok:
        return v

    # 4 — the new tests must FAIL on the old code. Copy the fixed test files
    # into the base tree and run just those tests there.
    for node in {n.split("::")[0] for n in v.new_tests}:
        (base / node).parent.mkdir(parents=True, exist_ok=True)
        (base / node).write_bytes((fixed / node).read_bytes())
    rc, out = run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *v.new_tests],
                  base, env, timeout=600)
    if rc == 0:
        v.fail("the new regression tests PASS on the unfixed code — they do not "
               "reproduce the fault")

    rc, out = run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x"], fixed, env)
    if rc != 0:
        v.fail("test suite fails on the fixed tree:\n" + out[-1500:])
    rc, out = run([python, "-m", "ruff", "check", "."], fixed, env, timeout=300)
    if rc != 0:
        v.fail("ruff fails on the fixed tree:\n" + out[-800:])
    return v
