"""Tests for :mod:`omnifocus.sync.webdav`."""

from __future__ import annotations

import pytest
import respx
import httpx

from omnifocus.errors import OFWebDAVError
from omnifocus.sync.webdav import WebDAVClient

BASE_URL = "https://dav.example.com/omnifocus/OmniFocus.ofocus/"

# Minimal PROPFIND response listing two ZIPs
_PROPFIND_RESPONSE = """\
<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/omnifocus/OmniFocus.ofocus/</d:href>
    <d:propstat>
      <d:prop><d:displayname>OmniFocus.ofocus</d:displayname></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/omnifocus/OmniFocus.ofocus/00000000000000=abc+xyz.zip</d:href>
    <d:propstat>
      <d:prop><d:displayname>00000000000000=abc+xyz.zip</d:displayname></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/omnifocus/OmniFocus.ofocus/20260322T154011=xyz+abc.zip</d:href>
    <d:propstat>
      <d:prop><d:displayname>20260322T154011=xyz+abc.zip</d:displayname></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


def _make_client() -> WebDAVClient:
    return WebDAVClient(BASE_URL, "user", "pass")


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_missing_all_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("OF_WEBDAV_URL", "OF_WEBDAV_USER", "OF_WEBDAV_PASS"):
            monkeypatch.delenv(v, raising=False)
        with pytest.raises(OFWebDAVError, match="OF_WEBDAV_URL"):
            WebDAVClient.from_env()

    def test_missing_pass_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "user")
        monkeypatch.delenv("OF_WEBDAV_PASS", raising=False)
        with pytest.raises(OFWebDAVError, match="OF_WEBDAV_PASS"):
            WebDAVClient.from_env()

    def test_missing_user_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.delenv("OF_WEBDAV_USER", raising=False)
        monkeypatch.delenv("OF_WEBDAV_PASS", raising=False)
        with pytest.raises(OFWebDAVError, match="OF_WEBDAV_USER"):
            WebDAVClient.from_env()

    def test_all_vars_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_WEBDAV_URL", BASE_URL)
        monkeypatch.setenv("OF_WEBDAV_USER", "u")
        monkeypatch.setenv("OF_WEBDAV_PASS", "p")
        client = WebDAVClient.from_env()
        assert client._base_url == BASE_URL

    def test_credentials_embedded_in_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """https://user:pass@host/path — no separate user/pass vars needed."""
        monkeypatch.setenv("OF_WEBDAV_URL", "https://alice:secret@dav.example.com/of/")
        monkeypatch.delenv("OF_WEBDAV_USER", raising=False)
        monkeypatch.delenv("OF_WEBDAV_PASS", raising=False)
        client = WebDAVClient.from_env()
        # Credentials must be stripped from the stored base URL
        assert "alice" not in client._base_url
        assert "secret" not in client._base_url
        assert client._base_url == "https://dav.example.com/of/"

    def test_explicit_vars_override_url_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OF_WEBDAV_USER / OF_WEBDAV_PASS take precedence over URL-embedded creds."""
        monkeypatch.setenv("OF_WEBDAV_URL", "https://wrong:wrong@dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "correct_user")
        monkeypatch.setenv("OF_WEBDAV_PASS", "correct_pass")
        # Should not raise; explicit vars win
        client = WebDAVClient.from_env()
        assert client._base_url == "https://dav.example.com/of/"

    def test_url_with_port_and_embedded_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Port must be preserved when stripping credentials from the URL."""
        monkeypatch.setenv("OF_WEBDAV_URL", "https://u:p@dav.example.com:8443/of/")
        monkeypatch.delenv("OF_WEBDAV_USER", raising=False)
        monkeypatch.delenv("OF_WEBDAV_PASS", raising=False)
        client = WebDAVClient.from_env()
        assert "8443" in client._base_url
        assert "u:p" not in client._base_url


# ---------------------------------------------------------------------------
# list_bundle
# ---------------------------------------------------------------------------


class TestListBundle:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_sorted_zip_names(self) -> None:
        respx.route(method="PROPFIND", url=BASE_URL).mock(
            return_value=httpx.Response(207, text=_PROPFIND_RESPONSE)
        )
        async with _make_client() as client:
            files = await client.list_bundle()

        assert "00000000000000=abc+xyz.zip" in files
        assert "20260322T154011=xyz+abc.zip" in files
        # must be sorted
        assert files == sorted(files)

    @pytest.mark.asyncio
    @respx.mock
    async def test_directory_entry_excluded(self) -> None:
        """The directory itself (href ends with /) must not appear in results."""
        respx.route(method="PROPFIND", url=BASE_URL).mock(
            return_value=httpx.Response(207, text=_PROPFIND_RESPONSE)
        )
        async with _make_client() as client:
            files = await client.list_bundle()
        assert not any(f.endswith("/") or "ofocus" == f for f in files)

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_raises(self) -> None:
        respx.route(method="PROPFIND", url=BASE_URL).mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(OFWebDAVError) as exc_info:
            async with _make_client() as client:
                await client.list_bundle()
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    @respx.mock
    async def test_invalid_xml_raises(self) -> None:
        respx.route(method="PROPFIND", url=BASE_URL).mock(
            return_value=httpx.Response(207, text="<broken xml")
        )
        with pytest.raises(OFWebDAVError, match="not valid XML"):
            async with _make_client() as client:
                await client.list_bundle()

    @pytest.mark.asyncio
    @respx.mock
    async def test_response_missing_href(self) -> None:
        """Responses without <href> must be silently skipped."""
        xml = """\
<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:propstat><d:prop/></d:propstat>
  </d:response>
</d:multistatus>
"""
        respx.route(method="PROPFIND", url=BASE_URL).mock(
            return_value=httpx.Response(207, text=xml)
        )
        async with _make_client() as client:
            files = await client.list_bundle()
        assert files == []


# ---------------------------------------------------------------------------
# get_file
# ---------------------------------------------------------------------------


class TestGetFile:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_bytes(self) -> None:
        url = BASE_URL + "00000000000000=abc+xyz.zip"
        respx.get(url).mock(return_value=httpx.Response(200, content=b"PKZIP"))
        async with _make_client() as client:
            data = await client.get_file("00000000000000=abc+xyz.zip")
        assert data == b"PKZIP"

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_raises(self) -> None:
        url = BASE_URL + "missing.zip"
        respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(OFWebDAVError) as exc_info:
            async with _make_client() as client:
                await client.get_file("missing.zip")
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# put_file
# ---------------------------------------------------------------------------


class TestPutFile:
    @pytest.mark.asyncio
    @respx.mock
    async def test_upload_success(self) -> None:
        url = BASE_URL + "20260322T160000=new+parent.zip"
        respx.put(url).mock(return_value=httpx.Response(201))
        async with _make_client() as client:
            await client.put_file("20260322T160000=new+parent.zip", b"PKZIP")
        # No exception = success

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_raises(self) -> None:
        url = BASE_URL + "tx.zip"
        respx.put(url).mock(return_value=httpx.Response(403))
        with pytest.raises(OFWebDAVError) as exc_info:
            async with _make_client() as client:
                await client.put_file("tx.zip", b"data")
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


class TestRetry:
    @pytest.mark.asyncio
    @respx.mock
    async def test_retries_on_503(self) -> None:
        url = BASE_URL + "file.zip"
        respx.get(url).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(200, content=b"ok"),
            ]
        )
        # Override delay to speed up tests
        import omnifocus.sync.webdav as wdav_module
        original = wdav_module._RETRY_BASE_DELAY
        wdav_module._RETRY_BASE_DELAY = 0.0
        try:
            async with _make_client() as client:
                data = await client.get_file("file.zip")
            assert data == b"ok"
        finally:
            wdav_module._RETRY_BASE_DELAY = original

    @pytest.mark.asyncio
    @respx.mock
    async def test_exhausted_retries_raises(self) -> None:
        url = BASE_URL + "file.zip"
        respx.get(url).mock(return_value=httpx.Response(503))
        import omnifocus.sync.webdav as wdav_module
        original = wdav_module._RETRY_BASE_DELAY
        wdav_module._RETRY_BASE_DELAY = 0.0
        try:
            with pytest.raises(OFWebDAVError) as exc_info:
                async with _make_client() as client:
                    await client.get_file("file.zip")
            assert exc_info.value.status_code == 503
        finally:
            wdav_module._RETRY_BASE_DELAY = original

    @pytest.mark.asyncio
    @respx.mock
    async def test_connection_error_retried(self) -> None:
        url = BASE_URL + "file.zip"
        respx.get(url).mock(
            side_effect=[
                httpx.ConnectError("timeout"),
                httpx.Response(200, content=b"data"),
            ]
        )
        import omnifocus.sync.webdav as wdav_module
        original = wdav_module._RETRY_BASE_DELAY
        wdav_module._RETRY_BASE_DELAY = 0.0
        try:
            async with _make_client() as client:
                data = await client.get_file("file.zip")
            assert data == b"data"
        finally:
            wdav_module._RETRY_BASE_DELAY = original

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_connection_errors_raises(self) -> None:
        url = BASE_URL + "file.zip"
        respx.get(url).mock(side_effect=httpx.ConnectError("timeout"))
        import omnifocus.sync.webdav as wdav_module
        original = wdav_module._RETRY_BASE_DELAY
        wdav_module._RETRY_BASE_DELAY = 0.0
        try:
            with pytest.raises(OFWebDAVError):
                async with _make_client() as client:
                    await client.get_file("file.zip")
        finally:
            wdav_module._RETRY_BASE_DELAY = original


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


class TestUrlNormalisation:
    def test_trailing_slash_added(self) -> None:
        client = WebDAVClient("https://dav.example.com/of", "u", "p")
        assert client._base_url.endswith("/")

    def test_trailing_slash_preserved(self) -> None:
        client = WebDAVClient(BASE_URL, "u", "p")
        assert client._base_url == BASE_URL
