"""Loads priors.yaml. Nothing else in the codebase reads that file.

§7.4 — "a confirmed bias updates the [v1 prior] in the playbook, dated in the
change log." In code that means: every tunable number lives in priors.yaml and
is reached through this module. A literal threshold anywhere else is a bug.
"""

from __future__ import annotations

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
