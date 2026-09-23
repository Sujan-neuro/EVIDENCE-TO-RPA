"""Where a run's artefacts live.

Everything one invocation produces -- the hypothesis, the IR, the generated
script, the execution logs, the summary -- belongs in one directory, named so
that runs sort in the order they happened. A hex id sorts arbitrarily and tells
a reader nothing; `run7` tells them it was the seventh.
"""
from __future__ import annotations

import re
from pathlib import Path

_RUN_RE = re.compile(r"^run(\d+)$")


def existing_runs(base: str | Path = "runs") -> list[Path]:
    base = Path(base)
    if not base.is_dir():
        return []
    numbered = [(int(m.group(1)), p) for p in base.iterdir()
                if p.is_dir() and (m := _RUN_RE.match(p.name))]
    return [p for _n, p in sorted(numbered)]


def next_run_dir(base: str | Path = "runs") -> Path:
    """`runs/run1`, then `runs/run2`, and so on.

    Numbered from what is already on disk rather than from a counter, so a run
    directory that was deleted or moved cannot cause the next run to overwrite
    one that still exists.
    """
    base = Path(base)
    used = {int(m.group(1)) for p in base.iterdir() if p.is_dir()
            and (m := _RUN_RE.match(p.name))} if base.is_dir() else set()
    n = 1
    while n in used:
        n += 1
    run = base / f"run{n}"
    run.mkdir(parents=True)
    return run
