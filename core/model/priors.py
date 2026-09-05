"""Loads priors.yaml. Nothing else in the codebase reads that file.

§7.4 — "a confirmed bias updates the [v1 prior] in the playbook, dated in the
change log." In code that means: every tunable number lives in priors.yaml and
is reached through this module. A literal threshold anywhere else is a bug.
"""

from __future__ import annotations

import contextlib
import functools
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "priors.yaml"


class Priors:
    """Dotted read-only access to priors.yaml.

    >>> p = Priors.load()
    >>> p.get("draft.queue_depth")
    12
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    @classmethod
    def load(cls, path: Path | str | None = None) -> Priors:
        p = Path(path) if path else _DEFAULT_PATH
        if not p.exists():
            raise FileNotFoundError(
                f"priors.yaml not found at {p}. Every threshold lives there; "
                "core refuses to run on hardcoded defaults (§7.4)."
            )
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")) or {})

    def get(self, dotted: str) -> Any:
        """Fetch by dotted path. Raises rather than defaulting — a missing prior
        is a doctrine gap, and silently substituting a number would hide it."""
        node: Any = self._raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(
                    f"prior '{dotted}' is not defined in priors.yaml. "
                    "Add it there (and to the playbook) rather than hardcoding a value."
                )
            node = node[part]
        return node

    def ladder(self, archetype: str) -> tuple[int, int]:
        """§5.3 — the FAAB bid range for an archetype, as % of original budget."""
        lo, hi = self.get(f"waivers.ladder.{archetype}")
        return int(lo), int(hi)

    def as_dict(self) -> dict[str, Any]:
        """For the agent packet — the model is told the thresholds it's bound by."""
        return self._raw


@functools.lru_cache(maxsize=1)
def priors() -> Priors:
    """Process-wide singleton. Cached so a run can't half-reload mid-decision."""
    return Priors.load()


@contextlib.contextmanager
def overridden(**dotted: Any):
    """Temporarily change priors, then restore them exactly.

    For the parameter sweep (scripts/optimize.py) and for tests. A
    coefficient that cannot be varied cannot be shown to be right, and the
    alternative — editing priors.yaml between runs — leaves the file wrong if a
    sweep dies halfway through.

    Deliberately NOT for production code: a live decision reads the committed
    priors or it is not reproducible. Double underscores stand in for dots,
    because keyword arguments cannot contain them:

        with overridden(draft__scarcity_weight=0.5): ...
    """
    p = priors()
    saved: list[tuple[dict, str, Any]] = []
    try:
        for key, value in dotted.items():
            parts = key.replace("__", ".").split(".")
            node: Any = p._raw
            for part in parts[:-1]:
                node = node[part]
            saved.append((node, parts[-1], node.get(parts[-1])))
            node[parts[-1]] = value
        yield p
    finally:
        for node, leaf, old in reversed(saved):
            if old is None:
                node.pop(leaf, None)
            else:
                node[leaf] = old
