"""Browser requests are strict snapshot readers, never upstream clients."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from matchplay_score import matchplay_api as api, web
from matchplay_score.snapshot_store import RedisStore


def snapshot(store):
    return {
        "fetched_at": store.now() - 10000,
        "tournament": {
            "arenas": [
                {"arenaId": 1, "name": "Stars", "tournamentArena": {"status": "active"}}
            ],
            "players": [{"playerId": 1, "name": "Saved Player"}],
        },
        "cards": [
            {
                "cardId": 1,
                "playerId": 1,
                "singlePlayerGames": [
                    {"arenaId": 1, "playerId": 1, "score": 1234},
                ],
            }
        ],
    }


@pytest.mark.parametrize("saved", [False, True])
def test_all_page_reads_never_invoke_publisher_or_matchplay(stores, saved):
    store = stores()
    if saved:
        import json

        store.command(
            "SET", store.key("tournament:281672"), json.dumps(snapshot(store))
        )
    paths = [
        "/ranking/281672",
        "/monitor/281672",
        "/overlay/281672",
        "/player/281672/Saved_Player",
        "/machines/281672",
        "/health/281672",
        "/how-it-works/281672",
        "/index",
        "/",
    ]
    with (
        patch.object(
            web, "Publisher", side_effect=AssertionError("A page must not publish")
        ),
        patch(
            "matchplay_score.publisher.requests.Session.get",
            side_effect=AssertionError("A page must not contact Match Play"),
        ) as upstream,
    ):
        for _ in range(3):
            with web.app.test_client() as client:
                for path in paths:
                    result = client.get(path, headers={"X-Requested-With": "ScoreHigh"})
                    assert result.status_code in {200, 503}, (path, result.status_code)
        upstream.assert_not_called()


def test_expired_snapshot_keeps_timestamp_and_survives_reader_restarts(stores):
    import json

    store = stores()
    old = snapshot(store)
    store.command("SET", store.key("tournament:281672"), json.dumps(old))
    original = api.get_tournament_snapshot(281672)
    api._cache.clear()
    assert api.get_tournament_snapshot(281672) == original
    assert api.snapshot_details(281672)["snapshot_age_seconds"] >= 10000
    assert api.snapshot_details(281672)["state"] == "cached"


def test_one_consistent_pair_is_used_for_each_browser_request(stores):
    import json

    store = stores()
    old = snapshot(store)
    store.command("SET", store.key("tournament:281672"), json.dumps(old))
    with web.app.test_request_context("/ranking/281672"):
        roster = api.get_tournament_data(281672)
        changed = deepcopy(old)
        changed["cards"] = []
        store.command("SET", store.key("tournament:281672"), json.dumps(changed))
        assert api.get_cards(281672) == old["cards"]
        assert roster == old["tournament"]
    assert api.get_cards(281672) == []


def test_shared_storage_failure_retains_local_snapshot_without_fetch(
    stores, monkeypatch
):
    import json

    store = stores()
    store.command("SET", store.key("tournament:281672"), json.dumps(snapshot(store)))
    api.get_tournament_snapshot(281672)
    broken = RedisStore(
        transport=lambda *args: (_ for _ in ()).throw(
            OSError("private-provider-details")
        )
    )
    monkeypatch.setattr(api, "get_store", lambda: broken)
    with patch(
        "matchplay_score.publisher.requests.Session.get",
        side_effect=AssertionError("No fallback fetch"),
    ):
        assert api.get_cards(281672)
        assert api.snapshot_details(281672)["state"] == "cache_unavailable"


def test_health_is_read_only_and_contains_no_credentials(stores):
    import json

    store = stores()
    old = snapshot(store)
    store.command("SET", store.key("tournament:281672"), json.dumps(old))
    before = store.command("DBSIZE")
    response = web.app.test_client().get("/health/281672")
    assert response.status_code == 200
    assert response.json["read_only"] is True
    assert response.json["snapshot_age_seconds"] >= 10000
    assert "no-store" in response.headers["Cache-Control"]
    assert store.command("DBSIZE") == before
    assert store.snapshot(281672) == old
