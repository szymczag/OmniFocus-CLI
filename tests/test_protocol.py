"""Tests for :mod:`omnifocus.sync.protocol`."""

from __future__ import annotations

import pytest

from omnifocus.errors import OFBundleNotFound
from omnifocus.sync.protocol import classify_bundle_files, client_id_from_filename, is_baseline


class TestClassifyBundleFiles:
    def test_separates_baseline_and_transactions(self) -> None:
        files = [
            "20260322T154011=xyz+abc.zip",
            "00000000000000=abc+xyz.zip",
            "20260101T000000=aaa+bbb.zip",
        ]
        baseline, txs = classify_bundle_files(files)
        assert baseline == "00000000000000=abc+xyz.zip"
        assert txs == sorted([
            "20260322T154011=xyz+abc.zip",
            "20260101T000000=aaa+bbb.zip",
        ])

    def test_transactions_sorted_lexicographically(self) -> None:
        files = [
            "00000000000000=base.zip",
            "20260322T160000=c.zip",
            "20260101T000000=a.zip",
            "20260201T000000=b.zip",
        ]
        _, txs = classify_bundle_files(files)
        assert txs == [
            "20260101T000000=a.zip",
            "20260201T000000=b.zip",
            "20260322T160000=c.zip",
        ]

    def test_no_baseline_raises(self) -> None:
        with pytest.raises(OFBundleNotFound):
            classify_bundle_files(["20260322T154011=xyz+abc.zip"])

    def test_empty_list_raises(self) -> None:
        with pytest.raises(OFBundleNotFound):
            classify_bundle_files([])

    def test_only_baseline_no_transactions(self) -> None:
        baseline, txs = classify_bundle_files(["00000000000000=abc.zip"])
        assert baseline == "00000000000000=abc.zip"
        assert txs == []


class TestIsBaseline:
    def test_baseline(self) -> None:
        assert is_baseline("00000000000000=abc+xyz.zip") is True

    def test_transaction(self) -> None:
        assert is_baseline("20260322T154011=xyz+abc.zip") is False

    def test_empty_string(self) -> None:
        assert is_baseline("") is False


class TestClientIdFromFilename:
    def test_transaction_with_plus(self) -> None:
        cid = client_id_from_filename("20260322T154011=clientABC+parentXYZ.zip")
        assert cid == "clientABC"

    def test_baseline_with_plus(self) -> None:
        cid = client_id_from_filename("00000000000000=clientABC+parentXYZ.zip")
        assert cid == "clientABC"

    def test_no_plus_returns_after_eq(self) -> None:
        cid = client_id_from_filename("00000000000000=clientABC.zip")
        assert cid == "clientABC"

    def test_no_eq_returns_none(self) -> None:
        assert client_id_from_filename("noeq.zip") is None

    def test_empty_after_eq_returns_none(self) -> None:
        assert client_id_from_filename("00000000000000=.zip") is None
