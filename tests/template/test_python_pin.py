"""The pinned Python must install on every Ubuntu image GitHub's runners use.

CI reads ``.python-version`` into ``actions/setup-python``, which only installs
versions built for the runner's OS image. When ``ubuntu-latest`` moved to a new
Ubuntu release, a patch pinned before that release had no build there and every
Python job failed at setup. Seeded repos copy this pin at ``make init``, so the
template's pin is what has to stay installable.

Pick the LOWEST patch that has a build on every current image, not the newest:
developers' ``uv`` installs the same pin locally, and an older ``uv`` only knows
the releases that existed when it was built.
"""

from __future__ import annotations

import json
import re
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PIN_FILES = (
    _REPO / ".python-version",
    _REPO / "stacks" / "python" / ".python-version",
)
_PYPROJECTS = (_REPO / "pyproject.toml", _REPO / "stacks" / "python" / "pyproject.toml")
_MANIFEST = (
    "https://raw.githubusercontent.com/actions/python-versions/main/"
    "versions-manifest.json"
)


def _pin() -> str:
    pins = {path.read_text().strip() for path in _PIN_FILES}
    assert len(pins) == 1, f"the .python-version files disagree: {pins}"
    return pins.pop()


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_both_python_version_files_pin_the_same_patch() -> None:
    assert re.fullmatch(r"3\.\d+\.\d+", _pin())


def test_the_pin_satisfies_requires_python() -> None:
    pin = _version(_pin())
    for path in _PYPROJECTS:
        spec = tomllib.loads(path.read_text())["project"]["requires-python"]
        for clause in spec.split(","):
            match = re.fullmatch(r"\s*(>=|<)\s*([\d.]+)\s*", clause)
            assert match, f"{path.name}: unexpected requires-python clause {clause!r}"
            op, bound = match.groups()
            ok = pin >= _version(bound) if op == ">=" else pin < _version(bound)
            assert ok, f"{path.name}: pin {_pin()} violates {spec!r}"


def _linux_images(entry: dict) -> set[str]:
    return {
        f["platform_version"]
        for f in entry["files"]
        if f["platform"] == "linux" and f["arch"] == "x64" and f.get("platform_version")
    }


def test_the_pin_installs_on_every_current_ubuntu_image() -> None:
    try:
        with urllib.request.urlopen(_MANIFEST, timeout=20) as response:  # nosec B310
            manifest = json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        pytest.skip(f"setup-python's version manifest is unreachable: {exc}")
    pin = _pin()
    minor = pin.rsplit(".", 1)[0] + "."
    releases = {
        e["version"]: _linux_images(e)
        for e in manifest
        if e["version"].startswith(minor) and re.fullmatch(r"[\d.]+", e["version"])
    }
    assert pin in releases, f"{pin} is not in setup-python's manifest"
    # The newest patch is built for exactly the images setup-python still supports;
    # older patches may list retired ones (e.g. 20.04).
    current = releases[max(releases, key=_version)]
    missing = current - releases[pin]
    covering = sorted(
        (v for v, images in releases.items() if images >= current), key=_version
    )
    assert not missing, (
        f"Python {pin} has no setup-python build for Ubuntu {sorted(missing)}, so CI "
        f"fails at setup on those runners. Pin the LOWEST patch built for every "
        f"image -- {covering[0] if covering else 'none yet'} -- in both "
        ".python-version files (an older local uv may not know newer patches)."
    )
