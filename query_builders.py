from __future__ import annotations


def average_completion_query(username: str) -> str:
    return f"players | filter uuid({username}) | extract nick average_completion"


def quicklook_query(player: str, season: int) -> str:
    index = f"index s{season}"
    player_filter = f"filter uuid({player})"
    timelines = f"| {index} | {player_filter} | to_timelines"

    def bastion(name: str) -> str:
        return (
            f'| label "For bastion {name}:" | {index} | {player_filter} '
            f"bastion({name}) | players | {player_filter} | "
            "extract tournament_fmt | quicksave "
        )

    return (
        f"{index} | {player_filter} | players | {player_filter} | "
        "extract tournament_fmt | quicksave "
        f"{timelines} | splits.get_if projectelo.timeline.reset | {player_filter} "
        "| count Resets | average time "
        f"{timelines} | splits.get_if projectelo.timeline.death | {player_filter} "
        "| count Deaths | average time "
        f"{timelines} | splits.get_if projectelo.timeline.death_spawnpoint | {player_filter} "
        "| count DeathResets | average time "
        f"{timelines} | splits.get_if nether.root | {player_filter} "
        "| count Nethers | average time "
        f"{timelines} | splits.get_if nether.find_bastion | {player_filter} "
        "| count Bastions | average time "
        f"{timelines} | splits.get_if nether.find_fortress | {player_filter} "
        "| count Fortresses | average time "
        f"{timelines} | splits.get_if story.follow_ender_eye | {player_filter} "
        "| count Strongholds | average time "
        f"{timelines} | splits.get_if story.enter_the_end | {player_filter} "
        "| count Ends | average time "
        + bastion("TREASURE")
        + bastion("STABLES")
        + bastion("BRIDGE")
        + bastion("HOUSING")
    )


def quicksplits_query(player: str, season: int) -> str:
    index = f"index s{season}"
    player_filter = f"filter uuid({player})"
    timelines = f"| {index} | {player_filter} | to_timelines"

    def split_difference(first: str, second: str) -> str:
        return (
            f"{timelines} | splits.diff {first} {second} | {player_filter} "
            f'| label "For {first} -> {second}:" '
            "| count SplitsCounted | average time"
        )

    return "".join(
        [
            split_difference("nether.root", "find_bastion"),
            split_difference("find_bastion", "find_fortress"),
            split_difference("find_fortress", "projectelo.timeline.blind_travel"),
            split_difference("find_fortress", "story.follow_ender_eye"),
            split_difference("story.follow_ender_eye", "end.root"),
            split_difference("end.root", "projectelo.timeline.dragon_death"),
        ]
    )


def leaderboard_query(
    value: str,
    season: int,
    *,
    player: str | None = None,
    seed_filter: str = "",
) -> str:
    index = f"index s{season}"
    prefix = f"{index} | {seed_filter}"
    player_suffix = "@player" if player is not None else ""
    key = value + player_suffix

    queries = {
        "pb": "filter noff | sort duration | take 10 | extract id date winner duration",
        "elo": "players | drop elo None() | rsort elo | take 10",
        "average_completion": (
            "players | drop average_completion None() | sort average_completion | "
            "take 10 | extract nick average_completion match_completions"
        ),
        "average_stronghold": _split_average_query(
            index, "story.follow_ender_eye", seed_filter
        )
        + " | sort 1 | take 10",
        "average_end": _split_average_query(index, "story.enter_the_end", seed_filter)
        + " | sort 1 | take 10",
    }

    if player is not None:
        queries.update(
            {
                "pb@player": (
                    f"filter noff | sort duration | enumerate | filter winner({player}) "
                    "| extract rql_dynamic id date winner duration"
                ),
                "elo@player": (
                    f"players | drop elo None() | rsort elo | enumerate | "
                    f"filter uuid({player}) | extract rql_dynamic uuid elo"
                ),
                "average_completion@player": (
                    "players | drop average_completion None() | sort average_completion | "
                    f"enumerate | filter uuid({player}) | extract rql_dynamic nick "
                    "average_completion match_completions"
                ),
                "average_stronghold@player": _split_average_player_query(
                    index, "story.follow_ender_eye", player, seed_filter
                ),
                "average_end@player": _split_average_player_query(
                    index, "story.enter_the_end", player, seed_filter
                ),
            }
        )

    try:
        body = queries[key]
    except KeyError as exc:
        raise ValueError(f"unknown leaderboard query: {key}") from exc

    # Split-average builders contain their own initial index because they
    # switch back to the match dataset after capturing the player set.
    if value in {"average_stronghold", "average_end"}:
        return body
    return prefix + body


def _split_average_query(index: str, event: str, seed_filter: str) -> str:
    return (
        f"{index} | {seed_filter}players lowff manygames | extract uuid | assign VP "
        f"| {index} | {seed_filter}keepifattrcontained uuid VP | extract timelines "
        f"| segmentby uuid | splits.get_if {event} | keepifattrcontained uuid VP "
        "| averageby time uuid"
    )


def _split_average_player_query(
    index: str, event: str, player: str | None, seed_filter: str
) -> str:
    assert player is not None
    return (
        f"{index} | filter uuid({player}) | players | extract uuid | assign target "
        f"| {index} | {seed_filter}players lowff manygames | extract uuid | assign VP "
        f"| {index} | {seed_filter}keepifattrcontained uuid VP | extract timelines "
        f"| segmentby uuid | splits.get_if {event} | keepifattrcontained uuid VP "
        "| averageby time uuid | keepifattrcontained 0 target"
    )


def matchup_query(
    player1: str, player2: str, season: int | None, to_extract: str
) -> str:
    query = (
        f"filter uuid({player1}) uuid({player2}) | players | "
        f"filter uuid({player1}) {to_extract}"
    )
    return f"index s{season} | {query}" if season is not None else query
