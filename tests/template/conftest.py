import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def template_tree(tmp_path: Path) -> Path:
    """Return a throwaway copy of the template repo (no .git, .venv, node_modules)."""
    dest = tmp_path / "repo"
    shutil.copytree(
        REPO_ROOT,
        dest,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "node_modules",
            "__pycache__",
            "*.pyc",
            "dist",
            "htmlcov",
            "worktrees",
        ),
    )
    return dest


def build_edge_skill(root: Path) -> Path:
    """Write the edge-case skill folder whose CLI hash is ``golden["edge-skill"]``.

    Built at test time because git would normalize the CRLF file, and because
    ``__pycache__`` and symlinks don't survive a checkout everywhere. Change it
    only together with the golden value.
    """
    skill = root / "edge-skill"
    (skill / "sub").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: edge-skill\n---\n\n# Edge\n")
    (skill / "crlf.md").write_bytes(b"line one\r\nline two\r\n")
    (skill / "binary.bin").write_bytes(b"\x00\x01\r\n\x02")  # NUL: not text, kept as is
    (skill / "Zeta.md").write_text("upper Z\n")
    (skill / "_under.md").write_text("underscore\n")
    (skill / "été.md").write_text("non-ASCII name\n")
    (skill / "sub" / "a.txt").write_text("sub a\n")
    # Excluded by the CLI: any file named metadata.json, and these directories.
    (skill / "metadata.json").write_text("{}\n")
    (skill / "sub" / "metadata.json").write_text("{}\n")
    for excluded in (".git", "__pycache__", "__pypackages__", "sub/__pycache__"):
        (skill / excluded).mkdir(parents=True)
        (skill / excluded / "x").write_text("excluded\n")
    # node_modules is hashed in lock version 2.
    (skill / "node_modules").mkdir()
    (skill / "node_modules" / "dep.js").write_text("module.exports = 1;\n")
    # Symlinks inside the folder count as their target; the rest are left out.
    os.symlink("Zeta.md", skill / "link.md")
    os.symlink("sub", skill / "linked-dir")
    os.symlink("..", skill / "sub" / "loop")  # a cycle back to the root
    os.symlink("missing.md", skill / "broken.md")
    outside = root / "outside.md"
    outside.write_text("outside the skill\n")
    os.symlink(outside, skill / "escape.md")
    return skill


@pytest.fixture
def edge_skill(tmp_path: Path) -> Path:
    """Build the edge-case skill folder whose CLI hash is ``golden.json["edge-skill"]``."""
    return build_edge_skill(tmp_path)
