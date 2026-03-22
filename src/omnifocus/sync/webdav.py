"""WebDAV client for OmniFocus sync server communication.

Provides async PROPFIND / GET / PUT operations against the WebDAV server
that hosts the ``.ofocus`` bundle.  All methods use exponential backoff with
up to three attempts on transient server errors (5xx).

Credentials are injected at construction time and must never be logged or
exposed in exception messages.

Usage::

    async with WebDAVClient.from_env() as client:
        files = await client.list_bundle()
        data = await client.get_file(files[0])
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator
from xml.etree import ElementTree as ET

import httpx

from omnifocus.errors import OFWebDAVError

# WebDAV PROPFIND response namespace
_DAV_NS = "{DAV:}"

# Maximum number of retry attempts for transient 5xx errors
_MAX_RETRIES = 3

# Base delay in seconds between retries (doubles each attempt)
_RETRY_BASE_DELAY = 0.5


class WebDAVClient:
    """Async WebDAV client scoped to a single ``.ofocus`` bundle directory.

    Args:
        base_url: Full URL to the directory containing ``.zip`` files,
            e.g. ``https://dav.example.com/omnifocus/OmniFocus.ofocus/``.
            A trailing slash is required.
        username: WebDAV Basic Auth username.
        password: WebDAV Basic Auth password.  Never logged.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 30.0,
    ) -> None:
        if not base_url.endswith("/"):
            base_url += "/"
        self._base_url = base_url
        self._client = httpx.AsyncClient(
            auth=(username, password),
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> WebDAVClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> WebDAVClient:
        """Construct a client from environment variables.

        Required variables:
            OF_WEBDAV_URL: Base URL ending with ``/``.
            OF_WEBDAV_USER: Username.
            OF_WEBDAV_PASS: Password.

        Raises:
            OFWebDAVError: If any required environment variable is missing.
        """
        missing = [
            v for v in ("OF_WEBDAV_URL", "OF_WEBDAV_USER", "OF_WEBDAV_PASS")
            if not os.environ.get(v)
        ]
        if missing:
            raise OFWebDAVError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
        return cls(
            base_url=os.environ["OF_WEBDAV_URL"],
            username=os.environ["OF_WEBDAV_USER"],
            password=os.environ["OF_WEBDAV_PASS"],
        )

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def list_bundle(self) -> list[str]:
        """List all ``.zip`` files in the bundle directory via PROPFIND.

        Returns:
            List of relative filenames (e.g. ``["00000000=abc.zip", "20260322=x.zip"]``),
            sorted lexicographically (chronological order for transaction ZIPs).

        Raises:
            OFWebDAVError: On HTTP error or parse failure.
        """
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:">'
            "  <d:prop><d:displayname/><d:getcontenttype/></d:prop>"
            "</d:propfind>"
        )
        response = await self._request(
            "PROPFIND",
            self._base_url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=body.encode("utf-8"),
        )

        try:
            root = ET.fromstring(response)
        except ET.ParseError as exc:
            raise OFWebDAVError(f"PROPFIND response is not valid XML: {exc}") from exc

        filenames: list[str] = []
        for resp_el in root.iter(f"{_DAV_NS}response"):
            href_el = resp_el.find(f"{_DAV_NS}href")
            if href_el is None or href_el.text is None:
                continue
            href = href_el.text.rstrip("/")
            filename = href.rsplit("/", 1)[-1]
            if filename.endswith(".zip"):
                filenames.append(filename)

        return sorted(filenames)

    async def get_file(self, filename: str) -> bytes:
        """Download a file from the bundle directory.

        Args:
            filename: Relative filename, e.g. ``"00000000=abc.zip"``.

        Returns:
            Raw bytes of the file.

        Raises:
            OFWebDAVError: On HTTP error.
        """
        url = self._base_url + filename
        return await self._request("GET", url)

    async def put_file(self, filename: str, data: bytes) -> None:
        """Upload a file to the bundle directory.

        Args:
            filename: Relative filename for the new transaction ZIP.
            data: Raw bytes to upload (ZIP archive, possibly encrypted).

        Raises:
            OFWebDAVError: On HTTP error.
        """
        url = self._base_url + filename
        await self._request(
            "PUT",
            url,
            headers={"Content-Type": "application/zip"},
            content=data,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> bytes:
        """Execute an HTTP request with exponential-backoff retry on 5xx.

        Args:
            method: HTTP method string.
            url: Full URL.
            headers: Additional request headers.
            content: Request body bytes, or ``None``.

        Returns:
            Response body bytes.

        Raises:
            OFWebDAVError: After all retries are exhausted, or on 4xx errors.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers or {},
                    content=content,
                )
            except httpx.RequestError as exc:
                last_exc = exc
                await asyncio.sleep(_RETRY_BASE_DELAY * (2**attempt))
                continue

            if response.status_code < 300:
                return response.content

            # 4xx errors are not retryable
            if 400 <= response.status_code < 500:
                raise OFWebDAVError(
                    f"{method} {url} failed with status {response.status_code}",
                    status_code=response.status_code,
                )

            # 5xx: retry
            last_exc = OFWebDAVError(
                f"{method} {url} failed with status {response.status_code}",
                status_code=response.status_code,
            )
            await asyncio.sleep(_RETRY_BASE_DELAY * (2**attempt))

        if isinstance(last_exc, OFWebDAVError):
            raise last_exc
        raise OFWebDAVError(f"{method} {url} failed: {last_exc}") from last_exc
