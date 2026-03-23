"""Tests for :mod:`omnifocus.launcher`."""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

from unittest.mock import patch

import click
import pytest

from omnifocus.launcher import console_main, main


class TestLauncher:
    def test_no_args_dispatches_to_mcp(self) -> None:
        with patch("omnifocus.launcher.mcp_main") as mock_mcp:
            main([])
        mock_mcp.assert_called_once_with()

    def test_explicit_mcp_dispatches_to_mcp(self) -> None:
        with patch("omnifocus.launcher.mcp_main") as mock_mcp:
            main(["mcp"])
        mock_mcp.assert_called_once_with()

    def test_sync_dispatches_to_cli(self) -> None:
        with patch("omnifocus.launcher.cli.main") as mock_cli:
            main(["sync"])
        mock_cli.assert_called_once_with(args=["sync"], prog_name="of", standalone_mode=True)

    def test_help_dispatches_to_cli(self) -> None:
        with patch("omnifocus.launcher.cli.main") as mock_cli:
            main(["--help"])
        mock_cli.assert_called_once_with(args=["--help"], prog_name="of", standalone_mode=True)

    def test_version_dispatches_to_cli(self) -> None:
        with patch("omnifocus.launcher.cli.main") as mock_cli:
            main(["--version"])
        mock_cli.assert_called_once_with(args=["--version"], prog_name="of", standalone_mode=True)

    def test_legacy_of_syntax_is_rejected(self) -> None:
        with pytest.raises(click.ClickException, match="Drop the extra 'of'"):
            main(["of", "sync"])

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(click.ClickException, match="Unknown launcher mode or command"):
            main(["unknown"])

    def test_mcp_with_extra_args_is_rejected(self) -> None:
        with pytest.raises(click.ClickException, match="does not accept additional arguments"):
            main(["mcp", "extra"])

    def test_console_main_exits_cleanly_on_click_exception(self) -> None:
        with patch("omnifocus.launcher.main", side_effect=click.ClickException("boom")):
            with pytest.raises(SystemExit, match="1"):
                console_main()

    def test_console_main_runs_main_when_no_error(self) -> None:
        with patch("omnifocus.launcher.main") as mock_main:
            console_main()
        mock_main.assert_called_once_with()
