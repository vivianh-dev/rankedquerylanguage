from __future__ import annotations

import asyncio
import base64
import binascii
import math
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp


DEFAULT_BASE_URL = "https://rql.vivianh.dev/api/v1/legacy"
DEFAULT_TIMEOUT_SECONDS = 130.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


class RqlClientError(Exception):
    def __init__(self, public_message: str, *, status: int | None = None) -> None:
        super().__init__(public_message)
        self.public_message = public_message
        self.status = status


@dataclass(frozen=True)
class RqlAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class RqlResult:
    output: str
    attachment: RqlAttachment | None
    count: int
    request_id: str | None = None


class RqlClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if not api_key.strip():
            raise ValueError("RQL_API_KEY is required")
        self._api_key = api_key.strip()
        self._timeout = aiohttp.ClientTimeout(
            total=timeout_seconds,
            connect=min(timeout_seconds, 10.0),
            sock_connect=min(timeout_seconds, 10.0),
        )
        self._session: aiohttp.ClientSession | None = None
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        self._status_lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> RqlClient:
        raw_timeout = os.environ.get("RQL_API_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("RQL_API_TIMEOUT_SECONDS must be a number") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("RQL_API_TIMEOUT_SECONDS must be positive")

        return cls(
            os.environ.get("RQL_API_BASE_URL", DEFAULT_BASE_URL),
            os.environ.get("RQL_API_KEY", ""),
            timeout_seconds=timeout,
        )

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "accept": "application/json",
                    "x-api-key": self._api_key,
                },
                timeout=self._timeout,
                raise_for_status=False,
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def execute(self, query: str) -> RqlResult:
        body = await self._request_json(
            "POST",
            "/query",
            json={"query": query, "format": "discord_json"},
        )
        return _decode_result(body)

    async def submit(self, query: str) -> str:
        body = await self._request_json(
            "POST",
            "/jobs",
            json={"query": query, "format": "discord_json"},
            expected_statuses={202},
        )
        job_id = body.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise RqlClientError("RQL returned an invalid async job response.")
        return job_id

    async def job_status(self, job_id: str) -> dict[str, Any]:
        return await self._request_json("GET", f"/jobs/{job_id}")

    async def job_result(self, job_id: str) -> RqlResult:
        body = await self._request_json("GET", f"/jobs/{job_id}/results")
        return _decode_result(body)

    async def status(self, *, max_age_seconds: float = 30.0) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._status_cache
        if cached is not None and now - cached[0] <= max_age_seconds:
            return cached[1]

        async with self._status_lock:
            now = time.monotonic()
            cached = self._status_cache
            if cached is not None and now - cached[0] <= max_age_seconds:
                return cached[1]
            body = await self._request_json("GET", "/status")
            self._status_cache = (now, body)
            return body

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        session = self._session
        if session is None or session.closed:
            raise RuntimeError("RQL client has not been started")

        expected = expected_statuses or {200}
        try:
            async with session.request(
                method,
                self.base_url + path,
                json=json,
                allow_redirects=False,
            ) as response:
                if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                    raise RqlClientError("RQL returned a response that is too large.")
                raw = await _read_limited(response)
                try:
                    body = _json_object(raw)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise RqlClientError("RQL returned an invalid response.", status=response.status) from exc

                if response.status not in expected:
                    raise _http_error(response.status, body)
                return body
        except RqlClientError:
            raise
        except asyncio.TimeoutError as exc:
            raise RqlClientError("The RQL service did not respond in time. Please try again.") from exc
        except aiohttp.ClientError as exc:
            raise RqlClientError("The RQL service is temporarily unavailable. Please try again.") from exc


def _json_object(raw: bytes) -> dict[str, Any]:
    import json

    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON response is not an object")
    return value


async def _read_limited(response: aiohttp.ClientResponse) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise RqlClientError("RQL returned a response that is too large.")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_result(body: dict[str, Any]) -> RqlResult:
    output = body.get("output", "")
    if not isinstance(output, str):
        raise RqlClientError("RQL returned an invalid result.")

    raw_count = body.get("count", 0)
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0

    attachment = None
    raw_attachment = body.get("attachment")
    if raw_attachment is not None:
        if not isinstance(raw_attachment, dict):
            raise RqlClientError("RQL returned an invalid attachment.")
        filename = raw_attachment.get("filename")
        content_type = raw_attachment.get("contentType")
        encoded = raw_attachment.get("dataBase64")
        if not all(isinstance(value, str) for value in (filename, content_type, encoded)):
            raise RqlClientError("RQL returned an invalid attachment.")
        if len(encoded) > ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4:
            raise RqlClientError("The query result is too large for Discord.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RqlClientError("RQL returned an invalid attachment.") from exc
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise RqlClientError("The query result is too large for Discord.")
        attachment = RqlAttachment(filename, content_type, data)

    request_id = body.get("requestId")
    return RqlResult(
        output=output,
        attachment=attachment,
        count=count,
        request_id=request_id if isinstance(request_id, str) else None,
    )


def _http_error(status: int, body: dict[str, Any]) -> RqlClientError:
    if status == 400:
        output = body.get("output")
        if isinstance(output, str) and output:
            return RqlClientError(output, status=status)
        return RqlClientError("The query is invalid.", status=status)
    if status in {401, 403}:
        return RqlClientError(
            "The bot's RQL API key is missing or invalid. Please contact the bot operator.",
            status=status,
        )
    if status == 404:
        return RqlClientError("The RQL compatibility service is currently unavailable.", status=status)
    if status == 409:
        return RqlClientError("The query job is not ready yet.", status=status)
    if status == 429:
        return RqlClientError("RQL is busy right now. Please try again shortly.", status=status)
    if status >= 500:
        return RqlClientError("The RQL service is temporarily unavailable. Please try again.", status=status)
    return RqlClientError(f"RQL request failed (HTTP {status}).", status=status)


def _validate_base_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlparse(value)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("RQL_API_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and not loopback:
        raise ValueError("RQL_API_BASE_URL must use HTTPS except for loopback development")
    return value
