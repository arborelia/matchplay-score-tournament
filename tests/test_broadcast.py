from dataclasses import replace
from unittest.mock import patch

import requests
import pytest

from matchplay_score import broadcast, matchplay_api as api, web
from matchplay_score.examples import example_history, example_standings
from matchplay_score.tournament import Card, SinglePlayerGame
from matchplay_score.snapshot_store import RedisStore


def test_top_cards_are_personal_bests_and_exclude_invalid_and_voided():
    history = example_history()
    other = Card(
        "Other Player", [SinglePlayerGame(1, "Stars", "Other Player", 1, 1, "old")]
    )
    selected = broadcast.select_cards([*history, other], "top", 3)
    assert [card.card_id for card, rank in selected] == [2, 0]
    assert [rank for card, rank in selected] == [1, 2]
    assert len({card.player_name for card, rank in selected}) == len(selected)


def test_recent_order_uses_latest_game_submission_not_card_creation_or_total():
    history = example_history()
    progress = next(card for card in history if card.card_id == 4)
    older = next(card for card in history if card.card_id == 2)
    # A newly submitted score makes an older card the most recently updated card.
    older.games[-1] = replace(older.games[-1], created_at="2026-11-07T18:00:00Z")
    empty = Card("Empty", [], card_id=999, created_at="2026-12-01")
    selected = broadcast.select_cards([*history, empty], "recent", 5)
    assert [card.card_id for card, rank in selected] == [2, 4]
    assert selected[1][0] is progress
    assert all(rank == 1 for card, rank in selected)


def test_cycle_queues_recent_cards_then_top_three():
    entries = broadcast.overlay_entries(example_standings(), "cycle", 5)
    assert [section for card, rank, section in entries] == ["recent"] * 5 + ["top"] * 3
    assert [rank for card, rank, section in entries if section == "top"] == [1, 2, 3]
    client = web.app.test_client()
    recent = client.get("/examples/overlay/?mode=recent").text
    assert 'class="broadcast-rank"' not in recent
    assert 'class="broadcast-rank">#1' in client.get("/examples/overlay/?mode=top").text


def test_overlay_reads_one_complete_shared_snapshot():
    data = {
        "tournament": {
            "players": [{"playerId": 1, "name": "Test Player"}],
            "arenas": [
                {"arenaId": 1, "name": "Stars", "tournamentArena": {"status": "active"}}
            ],
        },
        "cards": [
            {
                "cardId": 1,
                "playerId": 1,
                "singlePlayerGames": [
                    {"arenaId": 1, "playerId": 1, "score": 1000},
                ],
            }
        ],
    }
    with patch.object(
        broadcast, "get_cached_tournament_snapshot", return_value=data
    ) as snapshot:
        cards = broadcast.tournament_cards(281672)
    snapshot.assert_called_once_with(281672)
    assert cards[0].computed_score() == 500000


@pytest.mark.parametrize(
    "age, expected", [(0, 200), (30, 200), (3601, 200), (None, 503)]
)
def test_obs_never_fetches_matchplay_even_when_cache_is_stale_or_missing(
    monkeypatch, age, expected
):
    monkeypatch.setattr(api, "time", lambda: 10000)
    monkeypatch.setattr(
        api,
        "get_store",
        lambda: RedisStore(),
    )
    if age is not None:
        api._cache["tournament:281672"] = {
            "fetched_at": 10000 - age,
            "tournament": {
                "players": [{"playerId": 1, "name": "Saved Player"}],
                "arenas": [
                    {
                        "arenaId": 1,
                        "name": "Stars",
                        "tournamentArena": {"status": "active"},
                    }
                ],
            },
            "cards": [
                {
                    "cardId": 1,
                    "playerId": 1,
                    "singlePlayerGames": [
                        {"arenaId": 1, "playerId": 1, "score": 1000},
                    ],
                }
            ],
        }
    with (
        patch.object(
            api,
            "get_tournament_snapshot",
            side_effect=AssertionError("OBS must not refresh"),
        ),
        patch.object(
            requests.sessions.Session,
            "get",
            side_effect=AssertionError("OBS must not call Match Play"),
        ) as network,
    ):
        response = web.app.test_client().get("/overlay/281672?mode=cycle")
    assert response.status_code == expected
    network.assert_not_called()
    assert ("Saved Player" in response.text) == (expected == 200)


def test_setup_and_demos_do_not_read_matchplay():
    client = web.app.test_client()
    with patch.object(
        broadcast,
        "get_cached_tournament_snapshot",
        side_effect=AssertionError("Unexpected API read"),
    ):
        for path in [
            "/obs/281672",
            "/examples/obs/",
            "/examples/overlay/?mode=top",
            "/examples/overlay/?mode=recent",
        ]:
            assert client.get(path).status_code == 200


def test_overlay_options_are_bounded_and_names_escaped():
    card = Card(
        "<script>bad()</script>",
        [SinglePlayerGame(1, "Stars", "Test", 1000, 500, "old")],
    )
    with patch.object(web, "tournament_cards", return_value=[card]):
        html = (
            web.app.test_client()
            .get("/overlay/281672?mode=other&seconds=-1&count=999")
            .text
        )
    assert 'data-interval="8"' in html
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html
    assert "<script>bad()</script>" not in html
    with web.app.test_request_context("/?mode=other&seconds=-1&count=999"):
        assert broadcast.overlay_options() == {
            "mode": "cycle",
            "count": 10,
            "seconds": 8,
        }
    with web.app.test_request_context("/?seconds=bad&count=bad&mode=top"):
        assert broadcast.overlay_options() == {"mode": "top", "count": 3, "seconds": 12}


def test_cold_outage_keeps_overlay_shell_for_automatic_recovery():
    with patch.object(web, "tournament_cards", side_effect=requests.Timeout):
        response = web.app.test_client().get("/overlay/281672")
    assert response.status_code == 503
    assert "Waiting for scores." in response.text
    assert "data-card-deck" in response.text
    assert "overlay.js" in response.text
