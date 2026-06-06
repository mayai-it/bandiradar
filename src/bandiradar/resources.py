"""Access to packaged runtime data (fixtures + example profiles).

Data lives INSIDE the package (``src/bandiradar/data/…``) and is reached via
:mod:`importlib.resources`, so ``--sample`` and the bundled example profiles work
identically from a source checkout AND from a ``pip``-installed wheel — never via
fragile ``Path(__file__).parents[...]`` walking off the source tree.

The returned objects are :class:`~importlib.resources.abc.Traversable`; they
expose ``.read_text()`` / ``.read_bytes()`` / ``.name`` / ``.iterdir()``, which is
all the rest of the engine needs.
"""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable

_DATA = files("bandiradar") / "data"


def fixture(name: str) -> Traversable:
    """A bundled fixture file, e.g. ``fixture("incentivi.json")``."""
    return _DATA / "fixtures" / name


def profiles_dir() -> Traversable:
    """The bundled example-profiles directory."""
    return _DATA / "profiles"


def profile(name: str) -> Traversable:
    """A bundled example profile by file name, e.g. ``profile("mayai.yaml")``."""
    return profiles_dir() / name


def profile_names() -> list[str]:
    """Bare names (no ``.yaml``) of the bundled example profiles, sorted."""
    return sorted(
        p.name[: -len(".yaml")]
        for p in profiles_dir().iterdir()
        if p.name.endswith(".yaml")
    )


def resolve_profile(path_or_name: str) -> Traversable | None:
    """Resolve a bundled example profile by name (with or without ``.yaml``).

    Returns the Traversable for a bundled profile, or ``None`` if the string is
    not a known example name (the caller then treats it as a filesystem path).
    """
    stem = path_or_name.removesuffix(".yaml")
    return profile(f"{stem}.yaml") if stem in profile_names() else None
