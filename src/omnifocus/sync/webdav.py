"""WebDAV client for OmniFocus sync server communication.

Provides async PROPFIND / GET / PUT operations against the WebDAV server
that hosts the ``.ofocus`` bundle.  All methods use exponential backoff with
up to three attempts on transient server errors (5xx).

Credentials are injected at construction time and must never be logged or
exposed in exception messages.

Usage::

    async with WebDAVClient.from_env() as client:
        files = await client.list_entries()
        data = await client.get_file(files[0])
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

import asyncio
import base64
import logging
from collections.abc import Generator
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx

from omnifocus.env import read_env_or_file
from omnifocus.errors import OFWebDAVError

log = logging.getLogger(__name__)

# WebDAV PROPFIND response namespace
_DAV_NS = "{DAV:}"

# Maximum number of retry attempts for transient 5xx errors
_MAX_RETRIES = 3

# Base delay in seconds between retries (doubles each attempt)
_RETRY_BASE_DELAY = 0.5


class _ChallengeAuth(httpx.Auth):
    """HTTP auth that tries Basic first, then falls back to Digest.

    Omni Sync Server advertises only ``WWW-Authenticate: Digest`` (no Basic),
    so a plain Basic auth tuple always 401s against it. This class sends the
    first request with Basic credentials and, on a 401 Digest challenge,
    transparently replays the request using ``httpx.DigestAuth``. Servers that
    accept Basic are unaffected (one preemptive Basic attempt, as before).
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._mode: str | None = None  # None = undecided, "basic", "digest"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        if self._mode == "digest":
            yield from httpx.DigestAuth(self._username, self._password).auth_flow(request)
            return

        token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode("ascii")
        request.headers["Authorization"] = f"Basic {token}"
        response = yield request

        challenge = (response.headers.get("www-authenticate") or "").lower()
        if response.status_code == 401 and "digest" in challenge:
            self._mode = "digest"
            request.headers.pop("Authorization", None)
            yield from httpx.DigestAuth(self._username, self._password).auth_flow(request)
            return

        # Not a Digest challenge: the received response is final. httpx's auth
        # protocol ends the flow here (no further yield) and returns the
        # response that was just sent in.
        self._mode = "basic"


def _resolve_redirect_url(base_url: str) -> str:
    """Resolve host-level HTTP redirects on the bundle URL.

    Omni Sync Server redirects ``sync.omnigroup.com`` to a backend host such
    as ``sync3.omnigroup.com``. httpx's DigestAuth does not interoperate with
    redirects (the digest is computed against the pre-redirect host and the
    backend rejects it), so the client pins the base URL to the final host
    up front. Redirects that change the request path are not followed
    (a login-wall redirect would otherwise poison the base URL).

    Errors are swallowed: on any failure the original URL is used unchanged.
    """
    try:
        probe = httpx.Client(follow_redirects=False, timeout=15.0)
    except Exception:  # pragma: no cover - client construction is trivial
        return base_url

    try:
        with probe:
            original_path = httpx.URL(base_url).path
            current = base_url
            for _ in range(5):
                response = probe.request("PROPFIND", current, headers={"Depth": "0"})
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("location")
                if not location:
                    break
                resolved = str(httpx.URL(current).join(location))
                if httpx.URL(resolved).path != original_path:
                    break
                current = resolved
            return current
    except httpx.RequestError:
        return base_url
    except Exception:  # pragma: no cover - defensive
        return base_url


class WebDAVClient:
    """Async WebDAV client scoped to a single ``.ofocus`` bundle directory.

    Args:
        base_url: Full URL to the directory containing bundle files,
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
            auth=_ChallengeAuth(username, password),
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

        Credentials can be supplied in two ways (the second takes precedence):

        1. Embedded in the URL: ``https://user:pass@dav.example.com/path/``
        2. Separate variables: ``OF_WEBDAV_USER`` and ``OF_WEBDAV_PASS``

        Required variables:
            OF_WEBDAV_URL: Base URL, optionally with embedded credentials.
            OF_WEBDAV_USER: Username (overrides URL-embedded user).
            OF_WEBDAV_PASS: Password (overrides URL-embedded password).

        Raises:
            OFWebDAVError: If ``OF_WEBDAV_URL`` is missing, or if credentials
                cannot be found in either the URL or the separate variables.
        """
        raw_url = read_env_or_file("OF_WEBDAV_URL", error_type=OFWebDAVError) or ""
        if not raw_url:
            raise OFWebDAVError("Missing required environment variable: OF_WEBDAV_URL")

        # Parse credentials embedded in the URL (https://user:pass@host/path)
        parsed = urlsplit(raw_url)
        url_user = parsed.username or ""
        url_pass = parsed.password or ""

        # Strip credentials from the URL so they are not sent twice
        if url_user or url_pass:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc += f":{parsed.port}"
            clean_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
        else:
            clean_url = raw_url

        # Explicit env vars override URL-embedded credentials
        username = read_env_or_file("OF_WEBDAV_USER", error_type=OFWebDAVError) or url_user
        password = read_env_or_file("OF_WEBDAV_PASS", error_type=OFWebDAVError) or url_pass

        missing = []
        if not username:
            missing.append("OF_WEBDAV_USER")
        if not password:
            missing.append("OF_WEBDAV_PASS")
        if missing:
            raise OFWebDAVError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(base_url=_resolve_redirect_url(clean_url), username=username, password=password)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def list_entries(self) -> list[str]:
        """List all files in the bundle directory via PROPFIND.

        Returns:
            List of relative filenames (for example ``["00000000=abc.zip", "20260322=x.client"]``),
            sorted lexicographically.

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
            root = ET.fromstring(response)  # noqa: S314
        except ET.ParseError as exc:
            raise OFWebDAVError(f"PROPFIND response is not valid XML: {exc}") from exc

        filenames: list[str] = []
        for resp_el in root.iter(f"{_DAV_NS}response"):
            href_el = resp_el.find(f"{_DAV_NS}href")
            if href_el is None or href_el.text is None:
                continue
            href = href_el.text.rstrip("/")
            filename = href.rsplit("/", 1)[-1]
            if not filename or filename.endswith(".ofocus") or filename == "data":
                continue
            if "." in filename:
                filenames.append(filename)

        return sorted(filenames)

    async def list_bundle(self) -> list[str]:
        """List all bundle ZIPs in the bundle directory via PROPFIND."""
        return [filename for filename in await self.list_entries() if filename.endswith(".zip")]

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
            filename: Relative filename for the uploaded bundle file.
            data: Raw bytes to upload.

        Raises:
            OFWebDAVError: On HTTP error.
        """
        url = self._base_url + filename
        content_type = "application/octet-stream"
        if filename.endswith(".zip"):
            content_type = "application/zip"
        elif filename.endswith(".client"):
            content_type = "application/xml"
        await self._request("PUT", url, headers={"Content-Type": content_type}, content=data)

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
            log.debug("%s %s (attempt %d/%d)", method, url, attempt + 1, _MAX_RETRIES)
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers or {},
                    content=content,
                )
            except httpx.RequestError as exc:
                log.debug("Request error: %s — retrying", exc)
                last_exc = exc
                await asyncio.sleep(_RETRY_BASE_DELAY * (2**attempt))
                continue

            log.debug("← %d (%d bytes)", response.status_code, len(response.content))
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
