"""Tests for :mod:`omnifocus.parser`."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

import pytest

from omnifocus.errors import OFParseError
from omnifocus.models import OFModel
from omnifocus.parser import (
    _bool,
    _idref,
    _int,
    _parse_dt_local,
    _parse_dt_utc,
    _text,
    build_model,
    load_xml_from_zip,
)
from tests.conftest import make_zip

import xml.etree.ElementTree as ET

NS = "{http://www.omnigroup.com/namespace/OmniFocus/v2}"

# ---------------------------------------------------------------------------
# load_xml_from_zip
# ---------------------------------------------------------------------------


class TestLoadXmlFromZip:
    def test_valid_zip(self, sample_zip: bytes) -> None:
        root = load_xml_from_zip(sample_zip)
        assert root.tag == f"{NS}omnifocus"

    def test_not_a_zip(self) -> None:
        with pytest.raises(OFParseError, match="Invalid ZIP"):
            load_xml_from_zip(b"this is not a zip")

    def test_zip_without_contents_xml(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("other.xml", "<root/>")
        with pytest.raises(OFParseError, match="contents.xml not found"):
            load_xml_from_zip(buf.getvalue())

    def test_malformed_xml(self) -> None:
        zipped = make_zip("<unclosed>")
        with pytest.raises(OFParseError, match="Malformed XML"):
            load_xml_from_zip(zipped)

    def test_invalid_utf8(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("contents.xml", b"\xff\xfe broken bytes")
        with pytest.raises(OFParseError):
            load_xml_from_zip(buf.getvalue())


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

_SAMPLE_EL = ET.fromstring(
    f'<task xmlns="{NS[1:-1]}" id="t1">'
    f'  <name>Hello</name>'
    f'  <rank>42</rank>'
    f'  <flagged>true</flagged>'
    f'  <task idref="parent1"/>'
    f'</task>'
)


class TestHelpers:
    def test_text_present(self) -> None:
        assert _text(_SAMPLE_EL, "name") == "Hello"

    def test_text_missing(self) -> None:
        assert _text(_SAMPLE_EL, "nonexistent") == ""

    def test_text_default(self) -> None:
        assert _text(_SAMPLE_EL, "nonexistent", "fallback") == "fallback"

    def test_bool_true(self) -> None:
        assert _bool(_SAMPLE_EL, "flagged") is True

    def test_bool_missing(self) -> None:
        assert _bool(_SAMPLE_EL, "nonexistent") is False

    def test_bool_default_true(self) -> None:
        assert _bool(_SAMPLE_EL, "nonexistent", default=True) is True

    def test_int_present(self) -> None:
        assert _int(_SAMPLE_EL, "rank") == 42

    def test_int_missing(self) -> None:
        assert _int(_SAMPLE_EL, "nonexistent") is None

    def test_int_invalid(self) -> None:
        el = ET.fromstring(f'<task xmlns="{NS[1:-1]}"><rank>notanumber</rank></task>')
        assert _int(el, "rank") is None

    def test_idref_present(self) -> None:
        assert _idref(_SAMPLE_EL, "task") == "parent1"

    def test_idref_missing_child(self) -> None:
        assert _idref(_SAMPLE_EL, "folder") is None

    def test_idref_no_idref_attr(self) -> None:
        el = ET.fromstring(f'<task xmlns="{NS[1:-1]}"><context/></task>')
        assert _idref(el, "context") is None


class TestParseDt:
    def test_utc_with_z(self) -> None:
        dt = _parse_dt_utc("2026-03-22T15:40:11.347Z")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 22

    def test_utc_empty(self) -> None:
        assert _parse_dt_utc("") is None

    def test_utc_invalid(self) -> None:
        assert _parse_dt_utc("not-a-date") is None

    def test_local_present(self) -> None:
        dt = _parse_dt_local("2026-06-01T19:00:00.000")
        assert dt is not None
        assert dt.tzinfo is None
        assert dt.hour == 19

    def test_local_empty(self) -> None:
        assert _parse_dt_local("") is None

    def test_local_invalid(self) -> None:
        assert _parse_dt_local("bad") is None


# ---------------------------------------------------------------------------
# build_model — sample fixture
# ---------------------------------------------------------------------------


class TestBuildModel:
    def test_folders_loaded(self, sample_model: OFModel) -> None:
        assert "folder1" in sample_model.folders
        assert "folder2" in sample_model.folders
        assert sample_model.folders["folder1"].name == "Work"
        assert sample_model.folders["folder2"].parent_folder_id == "folder1"

    def test_tags_loaded(self, sample_model: OFModel) -> None:
        assert "tag1" in sample_model.tags
        assert "tag2" in sample_model.tags
        assert sample_model.tags["tag1"].name == "@home"
        assert sample_model.tags["tag2"].parent_tag_id == "tag1"

    def test_projects_loaded(self, sample_model: OFModel) -> None:
        assert "proj1" in sample_model.projects
        assert "proj2" in sample_model.projects
        p = sample_model.projects["proj1"]
        assert p.name == "🚀 Launch MVP"
        assert p.status == "active"
        assert p.flagged is True
        assert p.folder_id == "folder1"
        assert p.due is not None

    def test_project_singleton(self, sample_model: OFModel) -> None:
        p = sample_model.projects["proj2"]
        assert p.singleton is True
        assert p.folder_id is None
        assert p.status == "inactive"

    def test_project_completed(self, sample_model: OFModel) -> None:
        p = sample_model.projects["proj3"]
        assert p.status == "done"
        assert p.completed is not None

    def test_tasks_not_in_projects(self, sample_model: OFModel) -> None:
        # Project ids must not appear in tasks dict
        assert "proj1" not in sample_model.tasks
        assert "proj2" not in sample_model.tasks

    def test_task_loaded(self, sample_model: OFModel) -> None:
        t = sample_model.tasks["task1"]
        assert t.name == "Write tests"
        assert t.project_id == "proj1"
        assert t.parent_task_id == "proj1"
        assert t.flagged is False
        assert t.estimated_minutes == 120
        assert t.repetition_rule == "FREQ=WEEKLY;INTERVAL=1"
        assert "tag1" in t.tag_ids

    def test_task_completed(self, sample_model: OFModel) -> None:
        t = sample_model.tasks["task2"]
        assert t.completed is not None

    def test_nested_task_project_id(self, sample_model: OFModel) -> None:
        # task3 is a subtask of task1, which is under proj1
        t = sample_model.tasks["task3"]
        assert t.project_id == "proj1"
        assert t.parent_task_id == "task1"

    def test_hidden_task(self, sample_model: OFModel) -> None:
        t = sample_model.tasks["task4"]
        assert t.hidden is not None

    def test_inbox_task(self, sample_model: OFModel) -> None:
        t = sample_model.tasks["inbox1"]
        assert t.inbox is True
        assert t.project_id is None
        assert t.name == "Buy milk 🥛"

    def test_active_tasks(self, sample_model: OFModel) -> None:
        active = sample_model.active_tasks
        ids = {t.id for t in active}
        # task1, task3, inbox1 are active; task2 (completed) and task4 (hidden) are not
        assert "task1" in ids
        assert "task3" in ids
        assert "inbox1" in ids
        assert "task2" not in ids
        assert "task4" not in ids

    def test_no_transactions(self, sample_zip: bytes) -> None:
        model = build_model(sample_zip, transaction_bytes_list=None)
        assert len(model.tasks) > 0

    def test_empty_transaction_list(self, sample_zip: bytes) -> None:
        model = build_model(sample_zip, transaction_bytes_list=[])
        assert len(model.tasks) > 0


# ---------------------------------------------------------------------------
# Transaction merging
# ---------------------------------------------------------------------------

_BASE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="t1">
    <project/>
    <inbox>true</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Original name</name>
    <rank>1</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-01T00:00:00.000Z</modified>
  </task>
  <task id="t2">
    <project/>
    <inbox>true</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Will be deleted</name>
    <rank>2</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-01T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""

_TX_UPDATE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="t1">
    <project/>
    <inbox>true</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Updated name</name>
    <rank>1</rank>
    <flagged>true</flagged>
    <completed/>
    <modified>2026-01-02T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""

_TX_DELETE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="t2">
    <added>2026-01-02T00:00:00.000Z</added>
  </task>
</omnifocus>
"""

_TX_ADD_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="t3">
    <project/>
    <inbox>true</inbox>
    <added>2026-01-03T00:00:00.000Z</added>
    <name>Brand new task</name>
    <rank>3</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-03T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""

_TX_SECOND_UPDATE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="t1">
    <project/>
    <inbox>true</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Updated name again</name>
    <rank>5</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-03T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""

_TX_READD_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="t2">
    <project/>
    <inbox>true</inbox>
    <added>2026-01-04T00:00:00.000Z</added>
    <name>Restored task</name>
    <rank>2</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-04T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""

_TASK_TO_PROJECT_BASE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="shape1">
    <project/>
    <inbox>true</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Shape shifter</name>
    <rank>1</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-01T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""

_TASK_TO_PROJECT_TX_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="shape1">
    <name>Shape shifter</name>
    <rank>1</rank>
    <flagged>false</flagged>
    <added>2026-01-01T00:00:00.000Z</added>
    <modified>2026-01-02T00:00:00.000Z</modified>
    <project>
      <status>active</status>
      <singleton>false</singleton>
    </project>
  </task>
</omnifocus>
"""

_REPEATED_NAME_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="weekly-review-1">
    <project/>
    <inbox>true</inbox>
    <added>2026-03-01T00:00:00.000Z</added>
    <name>Weekly Review</name>
    <rank>1</rank>
    <flagged>false</flagged>
    <due>2026-03-22T19:00:00.000</due>
    <completed/>
    <modified>2026-03-01T00:00:00.000Z</modified>
  </task>
  <task id="weekly-review-2">
    <project/>
    <inbox>true</inbox>
    <added>2026-03-02T00:00:00.000Z</added>
    <name>Weekly Review</name>
    <rank>2</rank>
    <flagged>false</flagged>
    <due>2026-04-05T19:00:00.000</due>
    <completed/>
    <modified>2026-03-02T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""


class TestTransactionMerge:
    def test_update_overwrites_task(self) -> None:
        base = make_zip(_BASE_XML)
        tx = make_zip(_TX_UPDATE_XML)
        model = build_model(base, [tx])
        assert model.tasks["t1"].name == "Updated name"
        assert model.tasks["t1"].flagged is True

    def test_delete_removes_task(self) -> None:
        base = make_zip(_BASE_XML)
        tx = make_zip(_TX_DELETE_XML)
        model = build_model(base, [tx])
        assert "t2" not in model.tasks

    def test_add_new_task(self) -> None:
        base = make_zip(_BASE_XML)
        tx = make_zip(_TX_ADD_XML)
        model = build_model(base, [tx])
        assert "t3" in model.tasks
        assert model.tasks["t3"].name == "Brand new task"

    def test_multiple_transactions_applied_in_order(self) -> None:
        base = make_zip(_BASE_XML)
        txs = [make_zip(_TX_UPDATE_XML), make_zip(_TX_DELETE_XML), make_zip(_TX_ADD_XML)]
        model = build_model(base, txs)
        assert model.tasks["t1"].name == "Updated name"
        assert "t2" not in model.tasks
        assert "t3" in model.tasks

    def test_multiple_upserts_keep_latest_by_id(self) -> None:
        base = make_zip(_BASE_XML)
        txs = [make_zip(_TX_UPDATE_XML), make_zip(_TX_SECOND_UPDATE_XML)]
        model = build_model(base, txs)
        assert len(model.tasks) == 2
        assert model.tasks["t1"].name == "Updated name again"
        assert model.tasks["t1"].rank == 5

    def test_delete_followed_by_readd_restores_single_task(self) -> None:
        base = make_zip(_BASE_XML)
        txs = [make_zip(_TX_DELETE_XML), make_zip(_TX_READD_XML)]
        model = build_model(base, txs)
        assert model.tasks["t2"].name == "Restored task"
        assert list(model.tasks).count("t2") == 1

    def test_task_can_change_shape_into_project(self) -> None:
        base = make_zip(_TASK_TO_PROJECT_BASE_XML)
        tx = make_zip(_TASK_TO_PROJECT_TX_XML)
        model = build_model(base, [tx])
        assert "shape1" not in model.tasks
        assert model.projects["shape1"].name == "Shape shifter"
        assert model.projects["shape1"].status == "active"

    def test_repeated_names_remain_distinct_by_id(self) -> None:
        model = build_model(make_zip(_REPEATED_NAME_XML))
        weekly_reviews = [task for task in model.tasks.values() if task.name == "Weekly Review"]
        assert len(weekly_reviews) == 2
        assert {task.id for task in weekly_reviews} == {
            "weekly-review-1",
            "weekly-review-2",
        }


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_element_without_id_ignored(self) -> None:
        """Elements without an id attribute must be silently skipped."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task>
    <name>No ID task</name>
  </task>
  <folder>
    <name>No ID folder</name>
  </folder>
</omnifocus>
"""
        model = build_model(make_zip(xml))
        assert len(model.tasks) == 0
        assert len(model.folders) == 0

    def test_task_missing_name_ignored(self) -> None:
        """Tasks with no <name> child in the baseline are treated as deletions."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="ghost">
    <added>2026-01-01T00:00:00.000Z</added>
  </task>
</omnifocus>
"""
        model = build_model(make_zip(xml))
        assert "ghost" not in model.tasks

    def test_unicode_and_emoji_in_names(self) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="u1">
    <project/>
    <inbox>true</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Zrób zakupy 🛒 &amp; odpoczni 😴</name>
    <rank>1</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-01T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""
        model = build_model(make_zip(xml))
        assert model.tasks["u1"].name == "Zrób zakupy 🛒 & odpoczni 😴"

    def test_cycle_in_parent_chain_handled(self) -> None:
        """A circular parent reference must not cause infinite recursion."""
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2">
  <task id="a">
    <project/>
    <task idref="b"/>
    <inbox>false</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Task A</name>
    <rank>1</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-01T00:00:00.000Z</modified>
  </task>
  <task id="b">
    <project/>
    <task idref="a"/>
    <inbox>false</inbox>
    <added>2026-01-01T00:00:00.000Z</added>
    <name>Task B</name>
    <rank>2</rank>
    <flagged>false</flagged>
    <completed/>
    <modified>2026-01-01T00:00:00.000Z</modified>
  </task>
</omnifocus>
"""
        # Should not raise, project_id will be None for both
        model = build_model(make_zip(xml))
        assert model.tasks["a"].project_id is None
        assert model.tasks["b"].project_id is None
