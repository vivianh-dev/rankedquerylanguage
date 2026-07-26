from __future__ import annotations

import unittest
from pathlib import Path

import bot


class BotContractTest(unittest.TestCase):
    def test_existing_commands_are_preserved_and_async_is_added(self) -> None:
        names = {command.name for command in bot.client.tree.get_commands()}
        self.assertEqual(
            names,
            {
                "average_completion",
                "qb_info",
                "qb_quicklook",
                "qb_quicksplits",
                "qb_leaderboard",
                "qb_top_activity",
                "qb_matchup",
                "qb_faq",
                "query",
                "query-async",
            },
        )

    def test_runtime_has_no_klunk_dependency(self) -> None:
        for filename in ("bot.py", "rql_client.py", "async_jobs.py"):
            source = Path(filename).read_text(encoding="utf-8")
            self.assertNotIn("from klunk", source)
            self.assertNotIn("import klunk", source)


class QueryAsyncCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_command_acknowledges_ephemerally_and_persists_delivery(self) -> None:
        class FakeApi:
            async def submit(self, query):
                self.query = query
                return "job-1"

        class FakeStore:
            def add(self, job_id, user_id):
                self.saved = (job_id, user_id)

        class FakeResponse:
            async def defer(self, **kwargs):
                self.deferred = kwargs

        class FakeInteraction:
            def __init__(self):
                self.response = FakeResponse()
                self.user = type("User", (), {"id": 42})()

            async def edit_original_response(self, **kwargs):
                self.edited = kwargs

        previous_api = bot.client.rql
        previous_store = bot.client.async_jobs
        api = FakeApi()
        store = FakeStore()
        bot.client.rql = api
        bot.client.async_jobs = store
        interaction = FakeInteraction()
        try:
            await bot.query_async.callback(interaction, "| vars")
        finally:
            bot.client.rql = previous_api
            bot.client.async_jobs = previous_store

        self.assertEqual(interaction.response.deferred, {"ephemeral": True, "thinking": True})
        self.assertEqual(api.query, "| vars")
        self.assertEqual(store.saved, ("job-1", 42))
        self.assertIn("I’ll DM you", interaction.edited["content"])


if __name__ == "__main__":
    unittest.main()
