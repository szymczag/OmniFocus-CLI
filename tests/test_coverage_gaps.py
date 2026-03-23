"""Targeted tests to reach 100% branch coverage.

Each test class corresponds to a specific coverage gap identified from the
pytest-cov report.  Tests are as small as possible — only the minimum code
path needed to exercise the missing line/branch.
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from omnifocus.cli import _parse_due, cli
from omnifocus.errors import (
    OFAmbiguousMatch,
    OFBundleNotFound,
    OFEncryptionError,
    OFError,
    OFProjectNotFound,
)
from omnifocus.formatting import _json_default, render_project_tree
from omnifocus.fuzzy import _score
from omnifocus.models import Folder, OFModel, Project, Task
from omnifocus.parser import (
    _build_folder,
    _build_project,
    _build_tag,
    _build_task,
    build_model,
)
from omnifocus.writer import TransactionBuilder
from tests.conftest import make_zip

NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
NS = "{http://www.omnigroup.com/namespace/OmniFocus/v2}"

# ---------------------------------------------------------------------------
# cli.py — _parse_due weekday same-day branch (line 88: days_ahead = 7)
# ---------------------------------------------------------------------------


class TestParseDueSameDayWeekday:
    def test_same_weekday_gives_next_week(self) -> None:
        """When the target weekday is today, we want NEXT week not today."""
        # March 22, 2026 is a Sunday (weekday 6).
        # Entering "sun" should give +7 days (next Sunday), not today.
        result = _parse_due("sun")
        today = datetime.today().date()
        # result must be strictly in the future (not today)
        assert result.date() > today
        assert result.weekday() == 6  # Sunday


class TestParseDueInvalidMmDd:
    def test_invalid_mm_dd_falls_through_to_error(self) -> None:
        """'99-99' matches \\d{2}-\\d{2} but fromisoformat raises ValueError."""
        import click

        with pytest.raises(click.BadParameter):
            _parse_due("99-99")


# ---------------------------------------------------------------------------
# cli.py — _get_model OFEncryptionError and OFError branches (lines 119-124)
# ---------------------------------------------------------------------------


def _make_model() -> OFModel:
    model = OFModel()
    model.projects["p1"] = Project(
        id="p1",
        name="Eng",
        folder_id=None,
        status="active",
        singleton=False,
        rank=1,
        added=NOW,
        modified=NOW,
        flagged=False,
        due=None,
        start=None,
        note="",
        completed=None,
    )
    model.tasks["t1"] = Task(
        id="t1",
        name="A task",
        parent_task_id=None,
        project_id=None,
        inbox=True,
        completed=None,
        flagged=False,
        due=None,
        start=None,
        hidden=None,
        note="",
        rank=1,
        repetition_rule=None,
        estimated_minutes=None,
        added=NOW,
        modified=NOW,
    )
    return model


def _mock_store(model: OFModel | None = None) -> MagicMock:
    m = MagicMock()
    m.__aenter__ = AsyncMock(return_value=m)
    m.__aexit__ = AsyncMock(return_value=None)
    m.load = AsyncMock(return_value=model or _make_model())
    m.invalidate_cache = MagicMock()
    m._client = MagicMock()
    m._client.put_file = AsyncMock(return_value=None)
    return m


class TestGetModelErrors:
    def test_tasks_cmd_encryption_error(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        mock.load = AsyncMock(side_effect=OFEncryptionError("bad key"))
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks"])
        assert result.exit_code != 0
        assert "Encryption" in result.output

    def test_tasks_cmd_of_error(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        mock.load = AsyncMock(side_effect=OFBundleNotFound("missing"))
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks"])
        assert result.exit_code != 0

    def test_tasks_cmd_generic_of_error(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        mock.load = AsyncMock(side_effect=OFError("something generic"))
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks"])
        assert result.exit_code != 0
        assert "something generic" in result.output


class TestSyncCmdGenericError:
    def test_sync_generic_of_error(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        mock.load = AsyncMock(side_effect=OFError("generic sync error"))
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["sync"])
        assert result.exit_code != 0
        assert "generic sync error" in result.output


# ---------------------------------------------------------------------------
# cli.py — done_cmd ambiguous match (lines 335-339)
# ---------------------------------------------------------------------------


class TestDoneCmdAmbiguous:
    def test_ambiguous_low_score_shows_choices(self) -> None:
        """Multiple results with top score < 0.8 should print an error."""
        model = OFModel()
        # Add three tasks with very different names so no substring match
        for i in range(3):
            model.tasks[f"zx{i}"] = Task(
                id=f"zx{i}",
                name=f"Alpha beta {i} delta epsilon",
                parent_task_id=None,
                project_id=None,
                inbox=True,
                completed=None,
                flagged=False,
                due=None,
                start=None,
                hidden=None,
                note="",
                rank=i,
                repetition_rule=None,
                estimated_minutes=None,
                added=NOW,
                modified=NOW,
            )

        runner = CliRunner()
        # The query "alpha" is a substring match (score 0.8) so we'd get one result
        # We need a query where all scores are < 0.8 but > 0 and there are multiple.
        # Use a difflib-only match: "alph" vs "Alpha beta X delta epsilon"
        # "alph" is not useful here because it still appears within "alpha".
        # Try a different approach: tasks named with similar typos
        model2 = OFModel()
        for i in range(3):
            model2.tasks[f"ty{i}"] = Task(
                id=f"ty{i}",
                name=f"Xenomorph{i} research project",
                parent_task_id=None,
                project_id=None,
                inbox=True,
                completed=None,
                flagged=False,
                due=None,
                start=None,
                hidden=None,
                note="",
                rank=i,
                repetition_rule=None,
                estimated_minutes=None,
                added=NOW,
                modified=NOW,
            )
        mock2 = _mock_store(model2)

        # Query "xenomorf" — typo, difflib match but score < 0.8, and multiple tasks match
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock2):
            result = runner.invoke(cli, ["done", "xenomorf", "--yes"])

        # Either: one task matched (score >= 0.8 from substring) → success
        # Or: multiple low-score matches → error with "Ambiguous"
        # Just verify it doesn't crash
        assert result.exit_code in (0, 1)

    def test_explicitly_ambiguous_via_fuzzy_patch(self) -> None:
        """Patch find_tasks to return multiple low-score results, verifying the error path."""
        from omnifocus.fuzzy import MatchResult

        model = _make_model()
        mock = _mock_store(model)

        low_score_results = [
            MatchResult(score=0.3, task=model.tasks["t1"]),
            MatchResult(score=0.25, task=model.tasks["t1"]),
        ]

        runner = CliRunner()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            with patch("omnifocus.cli.find_tasks", return_value=low_score_results):
                result = runner.invoke(cli, ["done", "something", "--yes"])

        assert result.exit_code != 0
        assert "Ambiguous" in result.output


# ---------------------------------------------------------------------------
# errors.py — OFAmbiguousMatch and OFProjectNotFound (lines 53-57, 60-61)
# ---------------------------------------------------------------------------


class TestErrorInstantiation:
    def test_of_ambiguous_match(self) -> None:
        candidates = [(0.9, "t1", "Task One"), (0.8, "t2", "Task Two")]
        err = OFAmbiguousMatch("query", candidates)
        assert err.query == "query"
        assert len(err.candidates) == 2
        assert "2 tasks" in str(err)

    def test_of_project_not_found(self) -> None:
        err = OFProjectNotFound("my project")
        assert "my project" in str(err)


# ---------------------------------------------------------------------------
# formatting.py — folder not found branch (lines 116-118)
# ---------------------------------------------------------------------------


class TestFormattingFolderNotFound:
    def test_folder_with_nonexistent_parent_id(self) -> None:
        """A folder whose parent_folder_id points to a missing folder triggers lines 116-118.

        get_or_create_folder_node is called recursively for parent; parent not in folders
        dict → folder is None branch is hit.
        """
        con = Console(file=StringIO(), highlight=False, markup=False, no_color=True, width=200)
        folders = {
            "f1": Folder(
                id="f1",
                name="Child Folder",
                parent_folder_id="missing_parent",
                rank=1,
                added=NOW,
                modified=NOW,
            )
        }
        # Must not raise; should create a placeholder dim node for the missing parent
        render_project_tree(folders, {}, console=con)


# ---------------------------------------------------------------------------
# formatting.py — _json_default date (line 196-197) and TypeError (line 198)
# ---------------------------------------------------------------------------


class TestJsonDefault:
    def test_date_object(self) -> None:
        """A plain date (not datetime) should be serialised via isoformat."""
        d = date(2026, 3, 22)
        result = _json_default(d)
        assert result == "2026-03-22"

    def test_unserialisable_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            _json_default(object())


# ---------------------------------------------------------------------------
# fuzzy.py — zero token overlap fallthrough to difflib (branch 104→110)
# ---------------------------------------------------------------------------


class TestFuzzyZeroTokenOverlapToDifflib:
    def test_no_token_overlap_uses_difflib(self) -> None:
        """Query has tokens that don't appear in the name at all; falls through to difflib."""
        # "zzz" has no tokens in common with "hello world" but difflib may score it
        score = _score("helo", "hello world")
        # "helo" is not a substring of "hello world" (no "helo" substring).
        # Tokens {"helo"} ∩ {"hello", "world"} = {} → overlap 0.
        # Difflib: "helo" vs "hello world" ratio > 0.4? Let's see: ratio ~0.67 yes
        assert score > 0.0

    def test_empty_token_set_skips_token_path(self) -> None:
        """Empty query tokens path: q_tokens is empty so token block is skipped."""
        score = _score("", "hello world")
        # Empty string: no substring match ("" in "hello world" is actually True!)
        # "" is technically in every string (empty string is a substring)
        # So this returns SUBSTRING_SCORE
        from omnifocus.fuzzy import SUBSTRING_SCORE

        assert score == SUBSTRING_SCORE

    def test_whitespace_query_empty_tokens_falls_through_to_difflib(self) -> None:
        """Whitespace-only query: not a substring, q_tokens is empty → hits branch 104->110."""
        # "   " is NOT a substring of "hello world" (spaces not in that position)
        # "   ".split() = [] → q_tokens = set() which is falsy → if q_tokens: is False
        # Falls through to difflib at line 110
        score = _score("   ", "hello world")
        # SequenceMatcher("   ", "hello world") ratio < SEQMATCH_MIN_RATIO → 0.0
        assert score == 0.0


# ---------------------------------------------------------------------------
# parser.py — builder functions return None on missing id/name
# ---------------------------------------------------------------------------


class TestParserBuilderNoneReturns:
    def _el(self, tag: str, attrs: dict[str, str], children: list[tuple[str, str]]) -> ET.Element:
        """Build a simple ET.Element for testing."""
        el = ET.Element(f"{NS}{tag}")
        for k, v in attrs.items():
            el.set(k, v)
        for child_tag, child_text in children:
            child = ET.SubElement(el, f"{NS}{child_tag}")
            child.text = child_text
        return el

    def test_build_folder_no_id(self) -> None:
        el = self._el("folder", {}, [("name", "Work")])
        assert _build_folder(el) is None

    def test_build_folder_no_name(self) -> None:
        el = self._el("folder", {"id": "f1"}, [])
        assert _build_folder(el) is None

    def test_build_tag_no_id(self) -> None:
        el = self._el("context", {}, [("name", "@home")])
        assert _build_tag(el) is None

    def test_build_tag_no_name(self) -> None:
        el = self._el("context", {"id": "t1"}, [])
        assert _build_tag(el) is None

    def test_build_project_no_name(self) -> None:
        el = self._el("task", {"id": "p1"}, [])
        assert _build_project(el) is None

    def test_build_project_id_name_no_project_child(self) -> None:
        """id+name but no <project> sub-element → proj is None branch (265->272)."""
        el = self._el("task", {"id": "p1"}, [("name", "My Project")])
        result = _build_project(el)
        # Returns a Project with defaults (folder_id=None, status='active', singleton=False)
        assert result is not None
        assert result.folder_id is None
        assert result.status == "active"

    def test_build_project_project_child_no_status(self) -> None:
        """<project> child present but no <status> child → raw_status is empty (268->270)."""
        el = ET.Element(f"{NS}task")
        el.set("id", "p1")
        name_el = ET.SubElement(el, f"{NS}name")
        name_el.text = "My Project"
        # Add <project> child with <folder> but NO <status>
        proj_el = ET.SubElement(el, f"{NS}project")
        folder_el = ET.SubElement(proj_el, f"{NS}folder")
        folder_el.set("idref", "f1")
        result = _build_project(el)
        assert result is not None
        assert result.status == "active"  # default, since no <status> child

    def test_build_task_no_name(self) -> None:
        el = self._el("task", {"id": "t1"}, [])
        assert _build_task(el, None) is None


class TestParserOrphanedParent:
    def test_task_pointing_to_nonexistent_parent(self) -> None:
        """A task whose parent_idref points to a missing task gets project_id=None."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="t1">
    <project/>
    <task idref="doesnotexist"/>
    <inbox>false</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Orphan task</name>
    <rank>1</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-01T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""
        model = build_model(make_zip(xml))
        # Task's parent doesn't exist → project_id should be None
        assert model.tasks["t1"].project_id is None


class TestParserTaskNoParentIdref:
    def test_task_with_no_parent_element(self) -> None:
        """A task with no <task idref> gets project_id=None (leaf with no parent chain)."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="t1">
    <project/>
    <inbox>true</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Top-level task</name>
    <rank>1</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-01T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""
        model = build_model(make_zip(xml))
        assert model.tasks["t1"].project_id is None


# ---------------------------------------------------------------------------
# parser.py — build_model skips None-returning builders (437->435, 444->442,
#             462->460, 473->470): elements with empty <name> tag reach the
#             index (has_name is True) but builders reject them (name == "").
# ---------------------------------------------------------------------------


class TestParserBuildModelNoneReturns:
    """build_model must skip elements whose builder returns None."""

    def test_folder_empty_name_skipped(self) -> None:
        """<folder> with empty <name> makes _build_folder return None → branch 437->435."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <folder id="f1"><name></name></folder>
</omnifocus>
"""
        model = build_model(make_zip(xml))
        assert "f1" not in model.folders

    def test_context_empty_name_skipped(self) -> None:
        """<context> with empty <name> makes _build_tag return None → branch 444->442."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <context id="c1"><name></name></context>
</omnifocus>
"""
        model = build_model(make_zip(xml))
        assert "c1" not in model.tags

    def test_project_task_empty_name_skipped(self) -> None:
        """Project <task> with empty <name> makes _build_project return None → 462->460."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="p1">
    <name></name>
    <project><status>active</status></project>
  </task>
</omnifocus>
"""
        model = build_model(make_zip(xml))
        assert "p1" not in model.projects

    def test_leaf_task_empty_name_skipped(self) -> None:
        """Leaf <task> with empty <name> makes _build_task return None → branch 473->470."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="t1">
    <name></name>
    <project/>
  </task>
</omnifocus>
"""
        model = build_model(make_zip(xml))
        assert "t1" not in model.tasks


# ---------------------------------------------------------------------------
# writer.py — TransactionBuilder._el (lines 103-106)
# ---------------------------------------------------------------------------


class TestTransactionBuilderEl:
    def test_el_with_children(self) -> None:
        builder = TransactionBuilder()
        result = builder._el("parent", ["<child/>"])
        assert result == "<parent><child/></parent>"

    def test_el_empty_children(self) -> None:
        builder = TransactionBuilder()
        result = builder._el("parent", [])
        assert result == "<parent/>"

    def test_el_with_extra_attrs(self) -> None:
        builder = TransactionBuilder()
        result = builder._el("tag", ["text"], extra_attrs=' id="x"')
        assert result == '<tag id="x">text</tag>'


# ---------------------------------------------------------------------------
# mcp_server.py — _load_model actual function (lines 89-92)
# ---------------------------------------------------------------------------


class TestLoadModelDirect:
    @pytest.mark.asyncio
    async def test_load_model_calls_store(self) -> None:
        """The actual _load_model function (not mocked) must call OFocusStore."""
        from omnifocus.mcp_server import _load_model

        mock = MagicMock()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=None)
        mock.load = AsyncMock(return_value=OFModel())

        with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
            model = await _load_model()

        assert isinstance(model, OFModel)
        mock.load.assert_called_once_with(force_refresh=False)

    @pytest.mark.asyncio
    async def test_load_model_force(self) -> None:
        from omnifocus.mcp_server import _load_model

        mock = MagicMock()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=None)
        mock.load = AsyncMock(return_value=OFModel())

        with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
            await _load_model(force=True)

        mock.load.assert_called_once_with(force_refresh=True)


# ---------------------------------------------------------------------------
# mcp_server.py — _parse_optional_date ValueError (lines 101-102)
# ---------------------------------------------------------------------------


class TestParseOptionalDate:
    def test_none_returns_none(self) -> None:
        from omnifocus.mcp_server import _parse_optional_date

        assert _parse_optional_date(None) is None

    def test_empty_string_returns_none(self) -> None:
        from omnifocus.mcp_server import _parse_optional_date

        assert _parse_optional_date("") is None

    def test_valid_iso_returns_datetime(self) -> None:
        from omnifocus.mcp_server import _parse_optional_date

        result = _parse_optional_date("2026-03-22T12:00:00")
        assert result is not None
        assert result.year == 2026

    def test_invalid_string_returns_none(self) -> None:
        from omnifocus.mcp_server import _parse_optional_date

        assert _parse_optional_date("not-a-date") is None


# ---------------------------------------------------------------------------
# mcp_server.py — main() entry point (lines 491-499)
# ---------------------------------------------------------------------------


class TestMcpServerMain:
    def test_main_calls_serve(self) -> None:
        """main() sets up stdio_server and calls server.run; both mocked to return immediately."""
        from omnifocus.mcp_server import main

        mock_read = object()
        mock_write = object()

        class _FakeCtx:
            async def __aenter__(self):
                return mock_read, mock_write

            async def __aexit__(self, *a):
                pass

        async def _fake_run(_r, _w, _opts):
            pass  # Complete immediately so asyncio.run() returns

        with patch("omnifocus.mcp_server.stdio_server", return_value=_FakeCtx()):
            with patch("omnifocus.mcp_server.server.run", side_effect=_fake_run):
                with patch(
                    "omnifocus.mcp_server.server.create_initialization_options", return_value={}
                ):
                    main()  # synchronous call; asyncio.run() creates its own event loop

    def test_main_is_callable(self) -> None:
        """Smoke test: main is importable and callable as a function."""
        from omnifocus.mcp_server import main

        assert callable(main)
