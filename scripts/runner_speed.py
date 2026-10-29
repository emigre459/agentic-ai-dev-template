"""Measure how fast this machine runs a fixed workload, against per-class references.

The per-test speed guard (``check_test_speed.py``) states its budgets as seconds
on a typical runner of a named class, but it runs everywhere: a developer laptop,
a hosted CI runner, a bigger self-hosted one. This module times one deterministic
pure-Python workload and compares it with the time the runner class is known to
take, so the guard can scale a budget by :func:`factor` instead of hard-coding a
per-machine multiplier. A slower runner makes every test slower, alone or not, so
an unscaled budget fails untouched tests on a slow runner and goes easy on a fast
one.

The workload is deliberately pure Python (integer arithmetic and dict churn, no
C-accelerated calls such as ``sum`` or ``sorted``) so it tracks single-thread
interpreter speed, which is what most of a test's wall time is. For a JavaScript
test suite it stands in for single-thread CPU speed: good enough to scale a
budget.

The settings, including the reference seconds per runner class, live in the
``test_speed`` section of ``.github/pr-reviewer.json`` (the repository's one
stack-neutral settings file). Every guard run prints this machine's calibration,
so seeding a new runner class is one line there.

Stdlib only and runnable by path, with no package import::

    python3 scripts/runner_speed.py [--config .github/pr-reviewer.json] [--repeats 5]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The repository's stack-neutral settings file; the guard reads ``test_speed``.
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / ".github" / "pr-reviewer.json"
#: The settings section, and how messages name its references.
SECTION = "test_speed"
REFERENCES_KEY = f"{SECTION}.reference_seconds"
#: Names the runner class this process runs on (else ``default_runner_class``).
RUNNER_CLASS_ENV = "TEST_SPEED_RUNNER_CLASS"


class ConfigError(ValueError):
    """The ``test_speed`` section is missing, unreadable or malformed."""


#: Keys the ``test_speed`` section must carry (``_note`` and others are ignored).
REQUIRED_KEYS = (
    "budget_seconds",
    "recheck_fraction",
    "factor_low",
    "factor_high",
    "shared_setup_budget_seconds",
    "default_runner_class",
    "reference_seconds",
)

#: Outer iterations of the workload. Sized so one run is a fraction of a second.
_WORKLOAD_ROUNDS = 42_000
#: Inner iterations per round (integer arithmetic plus dict churn).
_WORKLOAD_INNER = 60


def _workload() -> int:
    """Run the fixed pure-Python workload once.

    Returns
    -------
    int
        A checksum, returned so the work cannot be optimised away and so a
        change to the workload shows up as a changed value.
    """
    checksum = 0
    for round_no in range(_WORKLOAD_ROUNDS):
        table: dict[int, int] = {}
        for i in range(_WORKLOAD_INNER):
            key = (i * 2654435761 + round_no) % 1009
            table[key] = table.get(key, 0) + i * i % 97
        for key, value in table.items():
            checksum = (checksum * 31 + key + value) % 1_000_003
    return checksum


def calibrate(repeats: int = 5) -> float:
    """Time the fixed workload and return the fastest run.

    Best-of-N rather than the mean: contention only ever makes a run slower, so
    the minimum is the least noisy estimate of the machine's own speed.

    Parameters
    ----------
    repeats : int
        How many times to run the workload.

    Returns
    -------
    float
        The best (smallest) wall-clock seconds over ``repeats`` runs.

    Raises
    ------
    ValueError
        If ``repeats`` is below 1.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        _workload()
        best = min(best, time.perf_counter() - start)
    return best


def _positive(value: object) -> bool:
    """Return whether ``value`` is a positive JSON number (``bool`` excluded).

    Parameters
    ----------
    value : object
        A decoded JSON value.

    Returns
    -------
    bool
        True for a positive ``int`` or ``float``.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _validate(section: object, where: str) -> dict[str, Any]:
    """Check the ``test_speed`` section's shape and return it.

    Parameters
    ----------
    section : object
        The decoded section.
    where : str
        The config path, for messages.

    Returns
    -------
    dict[str, Any]
        The section.

    Raises
    ------
    ConfigError
        Naming the offending ``test_speed`` key when the shape is wrong.
    """
    if not isinstance(section, dict):
        raise ConfigError(f"{where}: missing a {SECTION!r} object")
    missing = [key for key in REQUIRED_KEYS if key not in section]
    if missing:
        raise ConfigError(f"{where}: {SECTION} is missing {', '.join(missing)}")
    for key in ("budget_seconds", "shared_setup_budget_seconds", "factor_low"):
        if not _positive(section[key]):
            raise ConfigError(
                f"{where}: {SECTION}.{key} must be a positive number, "
                f"got {section[key]!r}"
            )
    fraction = section["recheck_fraction"]
    if not _positive(fraction) or fraction > 1:
        raise ConfigError(
            f"{where}: {SECTION}.recheck_fraction must be in (0, 1], got {fraction!r}"
        )
    low, high = section["factor_low"], section["factor_high"]
    if not _positive(high) or not low <= 1 <= high:
        raise ConfigError(
            f"{where}: {SECTION}.factor_low/factor_high must satisfy "
            f"0 < low <= 1 <= high, got {low!r}/{high!r}"
        )
    references = section["reference_seconds"]
    if not isinstance(references, dict) or not references:
        raise ConfigError(f"{where}: {REFERENCES_KEY} must be a non-empty object")
    for name, seconds in references.items():
        if not name or not _positive(seconds):
            raise ConfigError(
                f"{where}: {REFERENCES_KEY}[{name!r}] must be a positive number, "
                f"got {seconds!r}"
            )
    default = section["default_runner_class"]
    if not isinstance(default, str) or default not in references:
        raise ConfigError(
            f"{where}: {SECTION}.default_runner_class must name a class in "
            f"{REFERENCES_KEY}, got {default!r}"
        )
    return section


def load_settings(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """Read and validate the ``test_speed`` section of the settings file.

    Parameters
    ----------
    path : Path
        The settings file (``.github/pr-reviewer.json`` by default).

    Returns
    -------
    dict[str, Any]
        The validated section.

    Raises
    ------
    ConfigError
        If the file is missing or not JSON, or the section is absent or malformed.
    """
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: cannot read the settings file ({exc})") from exc
    if not isinstance(config, dict):
        raise ConfigError(f"{path}: expected a JSON object with a {SECTION!r} section")
    return _validate(config.get(SECTION), str(path))


def runner_class(settings: Mapping[str, Any]) -> str:
    """Return the runner class this process runs on.

    Parameters
    ----------
    settings : Mapping[str, Any]
        The ``test_speed`` section.

    Returns
    -------
    str
        The ``TEST_SPEED_RUNNER_CLASS`` environment variable, else the section's
        ``default_runner_class``.
    """
    return os.environ.get(RUNNER_CLASS_ENV) or str(settings["default_runner_class"])


def reference_for(
    name: str, settings: Mapping[str, Any], *, measured: float | None = None
) -> float:
    """Look up one runner class's reference seconds, failing loudly if unknown.

    Parameters
    ----------
    name : str
        The runner class, e.g. ``"ubuntu-latest"``.
    settings : Mapping[str, Any]
        The ``test_speed`` section.
    measured : float | None
        This machine's calibration, when already taken, so the error can quote
        the exact line to add.

    Returns
    -------
    float
        That class's reference calibration seconds.

    Raises
    ------
    SystemExit
        If ``name`` has no reference. The message names the config key and the
        measurement to add (or how to take one).
    """
    references: Mapping[str, Any] = settings["reference_seconds"]
    if name in references:
        return float(references[name])
    if measured is None:
        how = (
            f'add "{name}": <seconds> to {REFERENCES_KEY} in .github/pr-reviewer.json, '
            "using the calibrate_s that `python3 scripts/runner_speed.py` prints on "
            "that runner"
        )
    else:
        how = (
            f'add "{name}": {measured:.3f} to {REFERENCES_KEY} in '
            ".github/pr-reviewer.json (this runner's calibration, just measured)"
        )
    raise SystemExit(
        f"unknown runner class {name!r} (set via {RUNNER_CLASS_ENV}); {how}, "
        f"or use one of: {', '.join(sorted(references))}"
    )


def factor(measured_s: float, reference_s: float, *, low: float, high: float) -> float:
    """Return how much slower than the reference this machine is, clamped.

    Parameters
    ----------
    measured_s : float
        This machine's :func:`calibrate` seconds.
    reference_s : float
        The reference class's calibration seconds.
    low : float
        Smallest factor to return.
    high : float
        Largest factor to return.

    Returns
    -------
    float
        ``measured_s / reference_s`` limited to ``[low, high]``.

    Raises
    ------
    ValueError
        If ``reference_s`` is not positive (a division by zero or a negative
        factor) or ``low`` exceeds ``high`` (the clamp would silently return
        ``low``).
    """
    if reference_s <= 0:
        raise ValueError(f"reference_s must be positive, got {reference_s!r}")
    if low > high:
        raise ValueError(f"low ({low!r}) must not exceed high ({high!r})")
    return max(low, min(high, measured_s / reference_s))


def main(argv: list[str] | None = None) -> int:
    """Print this machine's runner class and calibration.

    Parameters
    ----------
    argv : list[str] | None
        Argument vector; ``None`` uses ``sys.argv``.

    Returns
    -------
    int
        0.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"runner_class={runner_class(settings)} "
        f"calibrate_s={calibrate(args.repeats):.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
