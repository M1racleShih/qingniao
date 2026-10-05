"""Distribution artifact audit: the sdist must carry only public code and
documentation — never tests, experiments or gitignored personal/agent-tool
directories — even when those directories exist in the working tree at
build time. The include patterns are root-anchored (gitignore semantics)
and the personal directories are also listed in the exclude set, so a
build from a dirty working tree still cannot leak them. The wheel is not
covered here because it ships only ``src/qingniao``."""

from __future__ import annotations

import tarfile
from pathlib import Path

from hatchling.builders.sdist import SdistBuilder

SENTINEL_DIRS = (
    ".agents/skills/api-design",
    ".claude/skills/code-optimizer",
    ".claude/skills/.trusted",
    ".kimi/skills/lint",
    ".pi/skills/observability",
    ".maintainer",
)


def _build_sdist_with_sentinels(tmp_path: Path) -> str:
    repo = Path(__file__).resolve().parent.parent
    project = tmp_path / "project"
    (project / "src/qingniao").mkdir(parents=True)
    (project / "src/qingniao/__init__.py").write_text("__version__ = '0.1.0'\n")
    (project / "src/qingniao/cli.py").write_text("")
    for name in ("README.md", "README.zh-CN.md", "LICENSE", "CONTRIBUTING.md"):
        (project / name).write_text(f"{name}\n")
    (project / "docs").mkdir()
    (project / "docs/api.md").write_text("# api\n")
    (project / "examples").mkdir()
    (project / "examples/README.md").write_text("# examples\n")
    (project / "tests").mkdir()
    (project / "tests/test_x.py").write_text("")
    (project / "experiments").mkdir()
    (project / "experiments/run.py").write_text("")
    for rel in SENTINEL_DIRS:
        (project / rel).mkdir(parents=True, exist_ok=True)
        (project / rel / "README.md").write_text(f"SENTINEL {rel}\n")
    # build from the actual committed packaging configuration so this test
    # tracks the real rules, not a duplicate
    (project / "pyproject.toml").write_text((repo / "pyproject.toml").read_text())
    builder = SdistBuilder(str(project))
    return next(builder.build(directory=str(project / "dist")))


def _sdist_names(tmp_path: Path) -> list[str]:
    return tarfile.open(_build_sdist_with_sentinels(tmp_path)).getnames()


def test_sdist_never_packages_personal_or_dev_directories(tmp_path):
    names = _sdist_names(tmp_path)
    joined = "\n".join(names)
    for forbidden in (
        "tests/",
        "experiments/",
        ".agents/",
        ".claude/",
        ".kimi/",
        ".pi/",
        ".maintainer/",
        "skills/",
    ):
        assert forbidden not in joined, f"sdist leaked a forbidden path: {forbidden!r}"


def test_sdist_keeps_all_public_files(tmp_path):
    names = _sdist_names(tmp_path)
    for expected in (
        "/README.md",
        "/README.zh-CN.md",
        "/LICENSE",
        "/CONTRIBUTING.md",
        "/pyproject.toml",
        "/docs/api.md",
        "/examples/README.md",
        "/src/qingniao/__init__.py",
    ):
        assert any(name.endswith(expected) for name in names), f"missing {expected}"


def test_sdist_readme_matching_is_root_anchored(tmp_path):
    names = sorted(n for n in _sdist_names(tmp_path) if n.endswith("README.md"))
    assert names == [
        "qingniao-0.1.0/README.md",
        "qingniao-0.1.0/examples/README.md",
    ], names
