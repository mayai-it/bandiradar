"""Typer CLI — a THIN shell over ``core`` (ARCHITECTURE.md §9).

Planned commands: ``profile``, ``sources``, ``fetch``, ``match``, ``watch``,
``mcp`` — each with ``--json`` and defaulting to offline sample mode. Contains
NO business logic: every command calls into ``core``.

TODO(Prompt 6): wire the commands to core end-to-end.
"""

import typer

app = typer.Typer(
    name="bandiradar",
    help="Monitor Italian public funding opportunities and rank them for your company.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """BandiRadar CLI (scaffold).

    TODO(Prompt 6): register the real commands (profile, sources, fetch, match,
    watch, mcp), each thin over ``core`` and with ``--json``. This no-op callback
    just makes the otherwise command-less app buildable so ``--help`` works.
    """


def main() -> None:
    """Console-script entry point (``bandiradar``)."""
    app()
