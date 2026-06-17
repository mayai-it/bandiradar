"""Guard against the `__version__` drift that bit us through 0.13–0.19: the package
exposed a hardcoded "0.12.0" while pyproject moved on. `__version__` now derives from
the installed package metadata (single source of truth); this pins it to pyproject."""

import tomllib
from pathlib import Path

import bandiradar


def test_version_is_resolved_not_placeholder():
    # importlib.metadata resolved a real version (the package is installed).
    assert bandiradar.__version__ != "0.0.0+unknown"


def test_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert bandiradar.__version__ == declared, (
        "bandiradar.__version__ drifted from pyproject — reinstall (uv sync) after a "
        "version bump; __version__ now derives from package metadata."
    )
