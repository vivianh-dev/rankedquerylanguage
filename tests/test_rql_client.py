from __future__ import annotations

import asyncio
import base64
import unittest

from aiohttp import web

from rql_client import RqlClient, RqlClientError


class RqlClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self.status_requests = 0
        app = web.Application()
        app.router.add_post("/api/v1/legacy/query", self.query)
        app.router.add_post("/api/v1/legacy/jobs", self.submit)
        app.router.add_get("/api/v1/legacy/jobs/{job_id}", self.job_status)
        app.router.add_get("/api/v1/legacy/jobs/{job_id}/results", self.job_result)
        app.router.add_get("/api/v1/legacy/status", self.status)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sockets = self.site._server.sockets
        self.base_url = f"http://127.0.0.1:{sockets[0].getsockname()[1]}/api/v1/legacy"
        self.client = RqlClient(self.base_url, "test-secret")
        await self.client.start()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.runner.cleanup()

    async def query(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.requests.append((request.method, request.headers.get("x-api-key", ""), payload))
        query = payload["query"]
        if query == "bad":
            return web.json_response(
                {"success": False, "error": "invalid request", "output": "**Error:** bad"},
                status=400,
            )
        if query == "server":
            return web.json_response({"success": False, "error": "unavailable"}, status=503)
        if query == "slow":
            await asyncio.sleep(0.2)
        return web.json_response(
            {
                "success": True,
                "output": "side output\n",
                "attachment": {
                    "filename": "result.txt",
                    "contentType": "text/plain; charset=utf-8",
                    "dataBase64": base64.b64encode(b"row\n").decode(),
                },
                "count": 1,
                "messages": [],
                "dataset": None,
            }
        )

    async def submit(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.requests.append((request.method, request.headers.get("x-api-key", ""), payload))
        return web.json_response(
            {"success": True, "id": "job-1", "status": "queued", "format": "discord_json"},
            status=202,
        )

    async def job_status(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "id": request.match_info["job_id"],
                "status": "completed",
                "format": "discord_json",
                "resultUrl": f"/api/v1/legacy/jobs/{request.match_info['job_id']}/results",
            }
        )

    async def job_result(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "output": "finished\n",
                "attachment": None,
                "count": 3,
                "messages": [],
                "dataset": None,
            }
        )

    async def status(self, _request: web.Request) -> web.Response:
        self.status_requests += 1
        return web.json_response(
            {
                "success": True,
                "enabled": True,
                "currentSeason": 12,
                "datasets": ["default", "all"],
            }
        )

    async def test_sync_result_preserves_output_attachment_and_api_key(self) -> None:
        result = await self.client.execute("+asfile | vars")
        self.assertEqual(result.output, "side output\n")
        self.assertEqual(result.attachment.data, b"row\n")
        self.assertEqual(result.count, 1)
        self.assertEqual(
            self.requests[-1],
            (
                "POST",
                "test-secret",
                {"query": "+asfile | vars", "format": "discord_json"},
            ),
        )

    async def test_safe_query_error_is_forwarded(self) -> None:
        with self.assertRaisesRegex(RqlClientError, r"\*\*Error:\*\* bad") as raised:
            await self.client.execute("bad")
        self.assertEqual(raised.exception.status, 400)

    async def test_server_error_becomes_a_friendly_error(self) -> None:
        with self.assertRaisesRegex(RqlClientError, "temporarily unavailable") as raised:
            await self.client.execute("server")
        self.assertEqual(raised.exception.status, 503)

    async def test_submit_poll_and_result(self) -> None:
        job_id = await self.client.submit("| vars")
        self.assertEqual(job_id, "job-1")
        status = await self.client.job_status(job_id)
        self.assertEqual(status["status"], "completed")
        result = await self.client.job_result(job_id)
        self.assertEqual(result.output, "finished\n")
        self.assertEqual(result.count, 3)

    async def test_status_is_cached(self) -> None:
        first = await self.client.status()
        second = await self.client.status()
        self.assertIs(first, second)
        self.assertEqual(first["currentSeason"], 12)
        self.assertEqual(self.status_requests, 1)

    async def test_timeout_becomes_a_friendly_error(self) -> None:
        short = RqlClient(self.base_url, "test-secret", timeout_seconds=0.05)
        await short.start()
        try:
            with self.assertRaisesRegex(RqlClientError, "did not respond in time"):
                await short.execute("slow")
        finally:
            await short.close()


if __name__ == "__main__":
    unittest.main()
