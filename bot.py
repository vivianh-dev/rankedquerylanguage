from __future__ import annotations

import asyncio
import logging
import os
from io import BytesIO
import time
from datetime import datetime, timezone

import discord
from discord import app_commands

from async_jobs import AsyncJobStore, AsyncJobSupervisor
from rql_client import RqlClient, RqlClientError, RqlResult
from query_builders import (
    average_completion_query,
    leaderboard_query,
    matchup_query,
    quicklook_query,
    quicksplits_query,
)


LOGGER = logging.getLogger("rql-bot")
NON_ABNORMAL_PLAYERS = {
    "lowk3y_",
    "dandannyboy",
    "7rowl",
    "v_strid",
    "NoFearr1337",
    "Oxidiot",
    "Waluyoshi",
}


class MyClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # A CommandTree is a special type that holds all the application command
        # state required to make it work. This is a separate class because it
        # allows all the extra state to be opt-in.
        # Whenever you want to work with application commands, your tree is used
        # to store and work with them.
        # Note: When using commands.Bot instead of discord.Client, the bot will
        # maintain its own tree instead.
        self.tree = app_commands.CommandTree(self)
        self.rql: RqlClient | None = None
        self.async_jobs: AsyncJobStore | None = None
        self._supervisor_task: asyncio.Task[None] | None = None

    async def setup_hook(self):
        self.rql = RqlClient.from_env()
        await self.rql.start()
        self.async_jobs = AsyncJobStore.from_env()
        poll_seconds = float(os.environ.get("RQL_ASYNC_POLL_SECONDS", "2"))
        supervisor = AsyncJobSupervisor(
            self.rql,
            self.async_jobs,
            self.deliver_async_result,
            poll_seconds=poll_seconds,
        )
        self._supervisor_task = asyncio.create_task(supervisor.run(), name="rql-async-job-supervisor")
        await self.tree.sync()

    async def close(self) -> None:
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
            self._supervisor_task = None
        if self.async_jobs is not None:
            self.async_jobs.close()
            self.async_jobs = None
        if self.rql is not None:
            await self.rql.close()
            self.rql = None
        await super().close()

    async def deliver_async_result(
        self,
        user_id: int,
        job_id: str,
        result: RqlResult | None,
        failed: bool,
    ) -> None:
        user = self.get_user(user_id)
        if user is None:
            user = await self.fetch_user(user_id)
        if failed:
            await user.send(f"Your RQL query `{job_id}` failed. Please try it again or contact the bot operator.")
            return
        assert result is not None
        await send_result(
            user.send,
            result,
            prefix=f"Your RQL query `{job_id}` finished:",
        )


intents = discord.Intents.default()
client = MyClient(intents=intents)


@client.event
async def on_ready():
    if client.user is None:
        print("Logged in as None?")
    else:
        print(f"Logged in as {client.user} (ID: {client.user.id})")


def get_rql() -> RqlClient:
    if client.rql is None:
        raise RqlClientError("The RQL service client is not configured.")
    return client.rql


async def defer_public(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=False, thinking=True)


async def current_season(interaction: discord.Interaction) -> int | None:
    await defer_public(interaction)
    try:
        status = await get_rql().status()
        season = status.get("currentSeason")
        if isinstance(season, int) and not isinstance(season, bool):
            return season
        raise RqlClientError("RQL did not report a current season.")
    except RqlClientError as exc:
        await interaction.followup.send(exc.public_message)
        return None


async def send_result(send, result: RqlResult, *, prefix: str = "") -> None:
    output = result.output
    content = f"{prefix} {output}".strip() or "Query completed with no output."
    file = None
    if result.attachment is not None:
        file = discord.File(BytesIO(result.attachment.data), filename="result.txt")
    elif len(content) > 2000:
        file = discord.File(BytesIO(output.encode("utf-8")), filename="result.txt")
        content = f"{prefix} Result attached.".strip()

    if len(content) > 2000:
        content = content[:1997] + "..."
    await send(content, file=file)


async def run_discord_query(
    interaction: discord.Interaction,
    query: str,
    notes: list[str] | None = None,
    no_query: bool = False,
):
    await defer_public(interaction)
    qpfx = f"From query: `{query}`: " if not no_query else ""
    started = time.monotonic()
    try:
        result = await get_rql().execute(query)
    except RqlClientError as exc:
        LOGGER.warning(
            "Synchronous RQL request failed status=%s duration_ms=%d",
            exc.status,
            int((time.monotonic() - started) * 1000),
        )
        message = f"{qpfx}\n{exc.public_message}" if exc.status == 400 else exc.public_message
        if len(message) > 2000:
            message = message[:1997] + "..."
        await interaction.followup.send(message)
        return

    LOGGER.info(
        "Synchronous RQL request completed count=%d duration_ms=%d",
        result.count,
        int((time.monotonic() - started) * 1000),
    )
    note_text = "" if notes is None else "\nNote: " + "\nNote: ".join(notes)
    literal = "" if not result.output else f"\n{result.output}"
    try:
        if result.attachment is not None:
            content = f"{qpfx}{note_text}{literal}"
            if len(content) > 2000:
                content = content[:1997] + "..."
            await interaction.followup.send(
                content or None,
                file=discord.File(BytesIO(result.attachment.data), "result.txt"),
            )
        else:
            content = f"{qpfx}{note_text}{literal}"
            if len(content) > 2000:
                await interaction.followup.send(
                    f"Your query has a result size of {len(content)} characters, which is too long. "
                    "Try with +asfile| at the start."
                )
            else:
                await interaction.followup.send(content or "Query completed with no output.")
    except discord.HTTPException as exc:
        LOGGER.warning("Discord rejected RQL result: status=%s", exc.status)
        await interaction.followup.send(
            "Your query finished, but Discord rejected the result. Try generating a smaller result set."
        )


@client.tree.command()
@app_commands.describe(
    username="The case-insensitive username of the player to get a current-season average completion time for."
)
async def average_completion(interaction: discord.Interaction, username: str):
    await run_discord_query(interaction, average_completion_query(username))


def _discord_timestamp(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


@client.tree.command()
async def qb_info(interaction: discord.Interaction):
    await defer_public(interaction)
    try:
        status = await get_rql().status(max_age_seconds=0)
    except RqlClientError as exc:
        await interaction.followup.send(f"QueryBot is active, but {exc.public_message[0].lower()}{exc.public_message[1:]}")
        return

    datasets = ", ".join(status.get("datasets", []))
    latest = status.get("latestLoadedMatch")
    latest_text = "unavailable"
    if isinstance(latest, dict):
        latest_text = str(latest.get("display") or latest.get("id") or "unavailable")
        timestamp = _discord_timestamp(latest.get("date"))
        if timestamp is not None:
            latest_text += f" (<t:{timestamp}:R>)"

    response = f"QueryBot active.\nExplicit datasets loaded: {datasets}"
    response += f"\nMost recent match loaded: {latest_text}"
    await interaction.followup.send(response)


def apply_season(season: int | None, query_str: str):
    if season is not None:
        return f"index s{season} | {query_str}"
    return query_str


@client.tree.command(description="Get results for a player (defaults to previous season)")
@app_commands.describe(
    player="The player to get previous-season stats for.",
    season="The season (default: previous)",
)
async def qb_quicklook(interaction: discord.Interaction, player: str, season: int | None = None):
    cs = await current_season(interaction)
    if cs is None:
        return
    season = season if season is not None else cs - 1
    await run_discord_query(interaction, quicklook_query(player, season), no_query=True)


@client.tree.command(description="Get split timing results for a player (defaults to previous season)")
@app_commands.describe(
    player="The player username or UUID",
    season="The season (default: previous)",
)
async def qb_quicksplits(interaction: discord.Interaction, player: str, season: int | None = None):
    cs = await current_season(interaction)
    if cs is None:
        return
    season = season if season is not None else cs - 1
    await run_discord_query(interaction, quicksplits_query(player, season), no_query=True)


@client.tree.command(description="Various dynamic leaderboards with multiple options")
@app_commands.choices(
    value=[
        app_commands.Choice(name="Fastest Completions", value="pb"),
        app_commands.Choice(name="Elo", value="elo"),
        app_commands.Choice(name="Average Completion", value="average_completion"),
        app_commands.Choice(name="Average Stronghold Entry", value="average_stronghold"),
        app_commands.Choice(name="Average End Entry", value="average_end"),
    ],
    seed_type=[
        app_commands.Choice(name="None", value=""),
        app_commands.Choice(name="Village", value="filter seed_type(village) | "),
        app_commands.Choice(name="Desert Temple", value="filter seed_type(desert_temple) | "),
        app_commands.Choice(name="Shipwreck", value="filter seed_type(shipwreck) | "),
        app_commands.Choice(name="Ruined Portal", value="filter seed_type(ruined_portal) | "),
        app_commands.Choice(name="Buried Treasure", value="filter seed_type(buried_treasure) | "),
    ],
)
@app_commands.describe(
    value="The type of leaderboard you want to generate.",
    season="The season to generate a leaderboard for. By default, the current season.",
    player="The player you want to get the leaderboard position of (otherwise gets top 10)",
)
async def qb_leaderboard(
    interaction: discord.Interaction,
    value: app_commands.Choice[str],
    season: int | None = None,
    player: str | None = None,
    seed_type: app_commands.Choice[str] | None = None,
):
    cs = await current_season(interaction)
    if cs is None:
        return
    sz = cs if season is None else season
    if seed_type is None:
        ststr = ""
    else:
        ststr = seed_type.value
    v = value.value
    try:
        query = leaderboard_query(
            v,
            sz,
            player=player,
            seed_filter=ststr,
        )
    except ValueError:
        await interaction.followup.send(f"Your value of {v} is not a valid choice.")
        return

    await run_discord_query(interaction, query)


@client.tree.command(description="See how many games have been completed recently by high-elo players.")
@app_commands.describe(
    elo_min="Minimum elo to scan for. Default: 1500. Minimum: 1000.",
    recent_count="Number of matches you want to scan for activity. Default: 100. Max: 100000.",
)
async def qb_top_activity(interaction: discord.Interaction, elo_min: int = 1500, recent_count: int = 100):
    elo_min = max(elo_min, 1000)
    recent_count = min(recent_count, 100000)
    cs = await current_season(interaction)
    if cs is None:
        return

    load_query = (
        f"take last {recent_count} | players | drop elo None() | drop elo lt({elo_min}) | extract uuid | assign highplayers"
    )
    find_query = f" | index s{cs} | take last {recent_count} | filter nodecay | keepifattrcontained uuid highplayers | rsort date | extract date pretty"

    await run_discord_query(interaction, load_query + find_query)


@client.tree.command(description="See stats for a given player/player matchup.")
@app_commands.describe(
    player1="The player you want to get the winrate / stats for.",
    player2="The player you want to get stats against.",
    season="Optional. Default to latest season.",
    to_extract="Optional. Default to | extract winrate.",
)
async def qb_matchup(
    interaction: discord.Interaction,
    player1: str,
    player2: str,
    season: int | None = None,
    to_extract: str = "| extract winrate",
):
    await run_discord_query(
        interaction,
        matchup_query(player1, player2, season, to_extract),
    )


FAQ = {
    "update_rate": {
        "name": "Auto Updating",
        "question": "How does the bot's auto-updating work?",
        "answer": "Matches are automatically pulled by ID from the Ranked API. For rate limit reasons, the match puller sometimes waits up to five minutes to pull new matches. Use `/qb_info` to see the latest match that has been ingested into the bot.",
    },
    "maintenance": {
        "name": "Ranked Maintenances",
        "question": "Do Ranked maintenance outages affect the bot?",
        "answer": "If the ranked servers are down, the bot will stop loading new matches. However, the query system will still work fine on all matches loaded up until that point. You can see the latest match available in the database with `/qb_info`.",
    },
    "about": {
        "name": "About the bot",
        "question": "Who made this bot / how does it work?",
        "answer": "This bot was written by DesktopFolder. It is a Discord front-end for the hosted RQL query service. The query language and compatibility compiler/runtime are designed from scratch, but based off of APLs (array programming languages) and query languages like Splunk or SQL.",
    },
    "averages": {
        "name": "/average_completion",
        "question": "How does `/average_completion` work?",
        "answer": "This command uses a query that finds a player’s average completion time for the current season (ranked matches only!). If you want to get average completion time for another season or a specific set of matches, you will need a to make a custom query with `/query`.",
    },
    "learn": {
        "name": "Learning the Language",
        "question": "How do I learn how to use the query language?",
        "answer": "That's the fun part - you don't! More seriously, the language is not particularly well documented. Your best bet is a mix of the following:\n- Reading the results of `/query help` and `/query help FN` for a variety of common functions;\n- Making sure you use `| attrs` to see what attributes are available on the type you're operating over;\n- **Trying out the prewritten queries, like `/average_completion` and `/qb_leaderboard`, and looking at the queries they use;**\n- If you're having difficulties, feel free to ping me, I don't mind :)",
    },
    "correctness": {
        "name": "Dataset Correctness",
        "question": "How correct is the dataset?",
        "answer": "I take data consistency and accuracy very seriously! However, there is a limit to what is possible considering matches are sometimes corrupted, cheated, or not available. Generally speaking, though, data should be completely correct, especially when operating on later seasons. Seasons 0 and 1 had more corrupted or cheated matches.\nIf you notice a data inconsistency, please let me know :)",
    },
    "cheat": {
        "name": "Cheated Match Filter",
        "question": "How does the 'cheated match filter' work?",
        "answer": f"The dataset includes some cheated (or corrupted?) matches. In all default indices ***except `index all`***, those matches are removed with the `noabnormal` filter. You can apply this filter to your `index all` searches with `| filter noabnormal`. These matches **are autodetected** by the hosted compatibility engine. Matches are considered abnormal in the following situation: They are completed, and their duration is less than 7 minutes, and the winner is not in {NON_ABNORMAL_PLAYERS}.",
    },
}

FAQ_CHOICES = [app_commands.Choice(name=v["name"], value=k) for k, v in FAQ.items()]


@client.tree.command(description="Some mediocre responses to RQL questions.")
@app_commands.choices(choice=FAQ_CHOICES)
@app_commands.describe(
    choice="The FAQ you want to access.",
)
async def qb_faq(interaction: discord.Interaction, choice: app_commands.Choice[str]):
    q = choice.value
    if q not in FAQ:
        return await interaction.response.send_message(f"Your choice of {q} is not in the FAQ. Likely a bug?")
    faq = FAQ[q]
    ans = faq["answer"]
    que = faq["question"]
    return await interaction.response.send_message(f"**FAQ: {que}**\n{ans}")


@client.tree.command(description="Make a dynamic query using RQL (see /qb_faq).")
@app_commands.describe(
    query="Your Ranked query string. See #docs for details.",
)
async def query(interaction: discord.Interaction, query: str):
    # Lint level one: Query level.
    import re

    lints = [
        (
            r"^players\s*\|\s*filter\s*nick\([\w\s]*\)\s*\|\s*extract (nick )?average_completion\s*$",
            "*Note: /average\\_completion has been added to simplify this.*",
        ),
        (
            r"keepifattrcontained uuid highplayers",
            "*Note: /qb_top_activity has been added to simplify this.*",
        ),
    ]
    notes = None
    for rxp, res in lints:
        if re.search(rxp, query) is not None:
            notes = notes or list()
            notes.append(res)
    await run_discord_query(interaction, query, notes)


@client.tree.command(
    name="query-async",
    description="Queue an RQL query and receive the result by DM when it finishes.",
)
@app_commands.describe(
    query="Your Ranked query string. See #docs for details.",
)
async def query_async(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        job_id = await get_rql().submit(query)
        if client.async_jobs is None:
            raise RqlClientError("The async query delivery store is not configured.")
        client.async_jobs.add(job_id, interaction.user.id)
    except RqlClientError as exc:
        await interaction.edit_original_response(content=exc.public_message)
        return
    except Exception:
        LOGGER.exception("Failed to persist an async RQL job")
        await interaction.edit_original_response(
            content="The query was submitted, but DM delivery could not be scheduled. " "Please contact the bot operator."
        )
        return

    LOGGER.info("Queued async RQL job id=%s user_id=%s", job_id, interaction.user.id)
    await interaction.edit_original_response(
        content=f"Queued RQL job `{job_id}`. I’ll DM you when it finishes. " "Make sure DMs from this bot are enabled."
    )


async def run_cli(api: RqlClient):
    # CLI version of the bot, so that we can test without a Discord bot.
    print_debug = False
    try:
        import readline  # noqa: F401  # pyright: ignore
    except ImportError:
        print(
            "Warning: Was unable to import the readline module. "
            "To get readline support on windows, try `pip install pyreadline`"
        )
    while True:
        try:
            query = (await asyncio.to_thread(input, "> ")).strip()
            if query == "+debug":
                print_debug = True
            elif query == "-debug":
                print_debug = False
            else:
                source = f"+debug | {query}" if print_debug else query
                try:
                    result = await api.execute(source)
                    print(result.output or "Query had no inline result because only a file was produced.")
                except RqlClientError as exc:
                    print(exc.public_message)
        except EOFError:
            break


async def run_cli_with_client() -> None:
    api = RqlClient.from_env()
    await api.start()
    try:
        await run_cli(api)
    finally:
        await api.close()


def discord_token() -> str:
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        return token.strip()
    with open("token.txt", encoding="utf-8") as token_file:
        return token_file.read().strip()


def main(args):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if "--fake" in args:
        asyncio.run(run_cli_with_client())
    else:
        client.run(discord_token(), log_handler=None)


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
