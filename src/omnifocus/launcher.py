"""Container launcher for OmniFocus CLI and MCP modes.

This module keeps native Python console scripts unchanged while making the
container UX simpler:

- no args -> MCP server mode
- ``mcp`` -> explicit MCP server mode
- CLI commands like ``sync`` or ``add`` -> Click CLI mode
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

import sys
from collections.abc import Sequence

import click

from omnifocus.cli import cli
from omnifocus.mcp_server import main as mcp_main

_CLI_FLAGS = {"--help", "-h", "--version"}
_CLI_COMMANDS = set(cli.commands)
_USAGE = """Usage:
  podman run --rm of sync
  podman run --rm of add "Buy milk"
  podman run --rm -i of
  podman run --rm -i of mcp
"""


def _is_cli_invocation(argv: Sequence[str]) -> bool:
    """Return True when *argv* should dispatch to the Click CLI."""
    return bool(argv) and (argv[0] in _CLI_FLAGS or argv[0] in _CLI_COMMANDS)


def _raise_usage_error(message: str) -> None:
    """Raise a ClickException with usage guidance for the container launcher."""
    raise click.ClickException(f"{message}\n\n{_USAGE}")


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch container arguments to the CLI or MCP server."""
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        mcp_main()
        return

    if args[0] == "mcp":
        if len(args) > 1:
            _raise_usage_error("The 'mcp' mode does not accept additional arguments.")
        mcp_main()
        return

    if args[0] == "of":
        _raise_usage_error(
            "Legacy container syntax detected. Drop the extra 'of' and run the subcommand directly."
        )

    if _is_cli_invocation(args):
        cli.main(args=args, prog_name="of", standalone_mode=True)
        return

    _raise_usage_error(f"Unknown launcher mode or command: {args[0]!r}")


def console_main() -> None:
    """Run the launcher with Click-style user-facing error handling."""
    try:
        main()
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc


if __name__ == "__main__":  # pragma: no cover
    console_main()
