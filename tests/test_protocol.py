"""Tests for :mod:`omnifocus.sync.protocol`."""

from __future__ import annotations

import pytest

from omnifocus.errors import OFBundleNotFound
from omnifocus.sync.protocol import (
    build_bundle_state,
    classify_bundle_files,
    client_id_from_filename,
    is_baseline,
    latest_transaction_ref,
    parent_id_from_filename,
    parse_baseline_filename,
    parse_client_state_filename,
    parse_delta_filename,
    parse_transaction_filename,
)


class TestClassifyBundleFiles:
    def test_separates_baseline_and_deltas(self) -> None:
        files = [
            "20260322154011=xyz+abc.zip",
            "00000000000000=snapshot+tail.zip",
            "20260101000000=aaa+bbb.zip",
            "20260322154111=device.client",
        ]
        baseline, txs = classify_bundle_files(files)
        assert baseline == "00000000000000=snapshot+tail.zip"
        assert txs == [
            "20260101000000=aaa+bbb.zip",
            "20260322154011=xyz+abc.zip",
        ]

    def test_no_baseline_raises(self) -> None:
        with pytest.raises(OFBundleNotFound):
            classify_bundle_files(["20260322154011=xyz+abc.zip"])


class TestFilenameParsing:
    def test_is_baseline(self) -> None:
        assert is_baseline("00000000000000=abc+xyz.zip") is True
        assert is_baseline("20260322154011=xyz+abc.zip") is False

    def test_client_id_from_filename(self) -> None:
        assert client_id_from_filename("20260322154011=head+tail.zip") == "head"
        assert client_id_from_filename("00000000000000=snapshot+tail.zip") == "snapshot"
        assert client_id_from_filename("invalid.zip") is None

    def test_parent_id_from_filename(self) -> None:
        assert parent_id_from_filename("20260322154011=head+tail.zip") == "tail"
        assert parent_id_from_filename("20260322154011=head.zip") is None
        assert parent_id_from_filename("invalid.zip") is None

    def test_parse_baseline_filename(self) -> None:
        baseline = parse_baseline_filename("00000000000000=snapshot123+tail123.zip")
        assert baseline is not None
        assert baseline.snapshot_id == "snapshot123"
        assert baseline.tail_id == "tail123"
        assert parse_baseline_filename("00000000000000=+tail123.zip") is None

    def test_parse_delta_filename(self) -> None:
        delta = parse_delta_filename("20260322154011=head123+tail123.zip")
        assert delta is not None
        assert delta.head_id == "head123"
        assert delta.parent_tail_id == "tail123"
        assert parse_delta_filename("bad=head123+tail123.zip") is None

    def test_parse_client_state_filename(self) -> None:
        client = parse_client_state_filename("20260322154011=client123.client")
        assert client is not None
        assert client.client_id == "client123"
        assert parse_client_state_filename("bad=client123.client") is None

    def test_parse_transaction_filename_returns_none_for_malformed_name(self) -> None:
        assert parse_transaction_filename("bad.zip") is None

    def test_parse_transaction_filename_returns_transaction_ref(self) -> None:
        tx = parse_transaction_filename("20260322154011=head123+tail123.zip")
        assert tx is not None
        assert tx.client_id == "head123"
        assert tx.parent_id == "tail123"


class TestBundleState:
    def test_build_bundle_state_parses_realistic_listing(self) -> None:
        state = build_bundle_state(
            [
                "active_object_hidden_dates.capability",
                "00000000000000=snapshot123+tail123.zip",
                "20260322154011=head123+tail123.zip",
                "20260322154500=clientA.client",
                "20260322154600=clientB.client",
                "encrypted",
            ]
        )
        assert state.baseline.snapshot_id == "snapshot123"
        assert state.current_tail_id == "tail123"
        assert [delta.head_id for delta in state.deltas] == ["head123"]
        assert [client.client_id for client in state.clients] == ["clientA", "clientB"]
        assert state.capabilities == ("active_object_hidden_dates",)
        assert state.other_entries == ("encrypted",)

    def test_latest_transaction_ref_returns_latest_delta(self) -> None:
        ref = latest_transaction_ref(
            [
                "00000000000000=base+tail.zip",
                "20260322154011=aaa+bbb.zip",
                "20260323154011=ccc+ddd.zip",
            ]
        )
        assert ref is not None
        assert ref.client_id == "ccc"
        assert ref.parent_id == "ddd"

    def test_latest_transaction_ref_returns_none_when_only_baseline(self) -> None:
        assert latest_transaction_ref(["00000000000000=base+tail.zip"]) is None
