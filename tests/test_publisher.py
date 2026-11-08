"""Exercise the actual Redis Lua coordination across independent worker clients."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from threading import Event
from unittest.mock import Mock, patch

import pytest

from matchplay_score import matchplay_api as api, web
from matchplay_score.publisher import Publisher, retry_after_seconds
from matchplay_score.snapshot_store import RedisStore, StorageUnavailable

ID = 281672
ROSTER = {
    "players": [{"playerId": 1, "name": "Saved Player"}],
    "arenas": [
        {"arenaId": 1, "name": "Stars", "tournamentArena": {"status": "active"}}
    ],
    "status": "started",
}
CARDS = [
    {
        "cardId": 1,
        "playerId": 1,
        "singlePlayerGames": [{"arenaId": 1, "playerId": 1, "score": 1234}],
    }
]


def response(status=200, data=None, *, retry=None, next_page=None):
    return Mock(
        status_code=status,
        headers={"Retry-After": retry} if retry is not None else {},
        json=lambda: {"data": data, "links": {"next": next_page}},
    )


def session(*responses):
    return Mock(headers={}, get=Mock(side_effect=list(responses)))


def publish(stores):
    store = stores()
    client = session(response(data=ROSTER), response(data=CARDS))
    assert Publisher(store, client).run(ID)["status"] == "published"
    return store


def age(store):
    saved = store.snapshot(ID)
    saved["fetched_at"] -= 26
    store.command("SET", store.key(f"tournament:{ID}"), json.dumps(saved))
    return saved


def expire_cooldown(store):
    state = store.state()
    state["cooldown"] = store.now() - 1
    store.command("SET", store.key("publisher"), json.dumps(state))


def test_429_is_persisted_across_workers_and_blocks_other_tournaments(stores):
    store = publish(stores)
    saved = age(store)
    client = session(response(429, retry="3600"))
    result = Publisher(stores(), client).run(ID)
    assert result["reason"] == "rate_limited"
    state = stores().state()
    assert state["status_code"] == 429
    assert state["rate_limited"] is True
    assert state["scope"] == "api_identity"
    assert state["cooldown"] >= store.now() + 3599
    assert state["failures"] == 1
    assert store.command("TTL", store.key("publisher")) == -1
    assert store.snapshot(ID) == saved
    api._cache.clear()
    for target in (ID, 282630):
        other = session()
        assert (
            Publisher(stores(), other).run(target, force=True)["reason"] == "cooldown"
        )
        other.get.assert_not_called()
    assert api.get_tournament_snapshot(ID)["fetched_at"] == saved["fetched_at"]


def test_http_date_retry_after_is_not_shortened_to_backoff_cap(stores):
    store = stores()
    deadline = datetime.now(UTC) + timedelta(hours=2)
    client = session(response(429, retry=format_datetime(deadline, usegmt=True)))
    Publisher(store, client).run(ID)
    assert store.state()["cooldown"] >= int(deadline.timestamp())


@pytest.mark.parametrize("value", [None, "bad", "nan", "inf", "-1", "1.5"])
def test_missing_or_invalid_retry_after_uses_exponential_backoff_and_jitter(
    stores, value
):
    store = stores()
    with patch("matchplay_score.publisher.uniform", return_value=1.1) as jitter:
        Publisher(store, session(response(429, retry=value))).run(ID)
        first = store.state()["cooldown"] - store.now()
        assert 65 <= first <= 66
        expire_cooldown(store)
        Publisher(stores(), session(response(429, retry=value))).run(ID)
        assert 131 <= store.state()["cooldown"] - store.now() <= 132
        assert jitter.call_count == 2
    assert store.state()["failures"] == 2


def test_expiry_allows_one_retry_and_successful_publication_clears_rate_state(stores):
    store = publish(stores)
    saved = age(store)
    Publisher(store, session(response(429, retry="60"))).run(ID)
    expire_cooldown(store)
    assert api.snapshot_details(ID)["state"] == "recovery_pending"
    assert store.state()["rate_limited"] is True
    # Roster is reused; only cards are requested on this refresh.
    good = session(response(data=CARDS))
    assert Publisher(stores(), good).run(ID)["status"] == "published"
    assert good.get.call_count == 1
    state = store.state()
    assert state["rate_limited"] is False
    assert state["failures"] == 0
    assert state["cooldown"] == 0
    assert store.snapshot(ID)["fetched_at"] > saved["fetched_at"]


def test_second_429_extends_persisted_cooldown(stores):
    store = stores()
    Publisher(store, session(response(429, retry="60"))).run(ID)
    first = store.state()["cooldown"]
    expire_cooldown(store)
    Publisher(stores(), session(response(429, retry="600"))).run(ID)
    assert store.state()["cooldown"] > first
    assert store.state()["failures"] == 2


@pytest.mark.parametrize(
    "status, reason",
    [
        (401, "authentication_error"),
        (403, "authorization_error"),
        (500, "upstream_error"),
    ],
)
def test_non_429_errors_are_not_claimed_as_rate_limits(stores, status, reason):
    store = stores()
    Publisher(store, session(response(status))).run(ID)
    state = store.state()
    assert state["reason"] == reason
    assert not state.get("rate_limited", False)
    assert state["status_code"] == status


def test_request_timeout_is_distinct_and_does_not_replace_complete_snapshot(stores):
    import requests

    store = publish(stores)
    saved = age(store)
    client = session()
    client.get.side_effect = requests.Timeout()
    Publisher(store, client).run(ID)
    assert store.state()["reason"] == "timeout"
    assert store.state()["status_code"] is None
    assert not store.state().get("rate_limited", False)
    assert store.snapshot(ID) == saved


def test_concurrent_publishers_do_not_wait_or_duplicate_requests(stores):
    entered = Event()
    release = Event()

    def upstream(url, **kwargs):
        if not url.endswith("/cards"):
            entered.set()
            assert release.wait(5)
            return response(data=ROSTER)
        return response(data=CARDS)

    leader = session()
    leader.get.side_effect = upstream
    with ThreadPoolExecutor(max_workers=6) as pool:
        active = pool.submit(Publisher(stores(), leader).run, ID)
        assert entered.wait(5)
        try:
            others = [session() for _ in range(5)]
            pending = [
                pool.submit(Publisher(stores(), client).run, ID) for client in others
            ]
            assert all(future.result(2)["reason"] == "busy" for future in pending)
            assert all(client.get.call_count == 0 for client in others)
        finally:
            release.set()
        assert active.result(5)["status"] == "published"
    assert leader.get.call_count == 2


def test_request_budgets_stop_before_partial_publication(stores):
    store = publish(stores)
    saved = age(store)
    page = response(data=CARDS, next_page="https://api.matchplay.events/api/page/2")
    client = session(page)
    result = Publisher(store, client, max_requests=1).run(ID)
    assert result["reason"] == "invocation_budget"
    assert client.get.call_count == 1
    assert store.snapshot(ID) == saved


def test_rolling_request_budget_is_shared_across_publishers(stores):
    store = publish(stores)
    age(store)
    blocked = session()
    result = Publisher(stores(), blocked, minute_budget=2).run(ID)
    assert result["reason"] == "budget"
    blocked.get.assert_not_called()
    assert store.state()["budget_retry_at"] > store.now()


def test_missing_storage_and_storage_write_errors_fail_closed(stores):
    unavailable = RedisStore(
        transport=lambda *args: (_ for _ in ()).throw(OSError("private detail"))
    )
    client = session()
    assert Publisher(unavailable, client).run(ID)["reason"] == "storage_failure"
    client.get.assert_not_called()
    store = stores()
    with patch.object(store, "fail", side_effect=StorageUnavailable("unavailable")):
        assert (
            Publisher(store, session(response(429))).run(ID)["reason"]
            == "storage_failure"
        )
    other = session()
    assert Publisher(stores(), other).run(ID)["reason"] == "interrupted"
    other.get.assert_not_called()


def test_failed_later_page_never_publishes_partial_card_list(stores):
    store = publish(stores)
    saved = age(store)
    client = session(
        response(data=CARDS, next_page="https://api.matchplay.events/api/page/2"),
        response(429, retry="120"),
    )
    assert Publisher(store, client).run(ID)["reason"] == "rate_limited"
    assert store.snapshot(ID) == saved


def test_completed_tournament_is_not_fetched_again(stores):
    store = stores()
    completed = {**ROSTER, "status": "completed"}
    assert (
        Publisher(store, session(response(data=completed), response(data=CARDS))).run(
            ID
        )["status"]
        == "published"
    )
    age(store)
    client = session()
    assert Publisher(stores(), client).run(ID)["reason"] == "completed"
    client.get.assert_not_called()


def test_stale_lease_holder_cannot_reserve_publish_or_release_new_lease(stores):
    store = stores()
    old, admitted = store.acquire(ID)
    assert admitted["status"] == "acquired"
    store.command("SET", store.key("lease"), "replacement", "EX", 120)
    _, result = store.reserve(old, "/api/tournaments/281672/cards", 12)
    assert result["status"] == "lease_lost"
    assert not store.publish(old, ID, {"tournament": ROSTER, "cards": CARDS})
    assert not store.release(old)
    assert store.command("GET", store.key("lease")) == "replacement"


def test_roster_is_refreshed_for_new_players_without_per_player_requests(stores):
    store = publish(stores)
    age(store)
    updated_cards = [
        {
            **CARDS[0],
            "playerId": 2,
            "singlePlayerGames": [{**CARDS[0]["singlePlayerGames"][0], "playerId": 2}],
        }
    ]
    updated_roster = {
        **ROSTER,
        "players": [*ROSTER["players"], {"playerId": 2, "name": "New Player"}],
    }
    client = session(response(data=updated_cards), response(data=updated_roster))
    assert Publisher(store, client).run(ID)["status"] == "published"
    assert client.get.call_count == 2
    assert all("/players/" not in call.args[0] for call in client.get.call_args_list)


def test_foreign_pagination_link_never_receives_request_or_credentials(stores):
    store = stores()
    client = session(
        response(data=ROSTER),
        response(data=CARDS, next_page="https://example.com/steal"),
    )
    assert Publisher(store, client).run(ID)["reason"] == "invalid_response"
    assert client.get.call_count == 2
    assert store.snapshot(ID) is None


def test_storage_error_details_are_not_exposed():
    store = RedisStore(
        transport=lambda *args: (_ for _ in ()).throw(
            OSError("secret-connection-string")
        )
    )
    with pytest.raises(StorageUnavailable) as error:
        store.state()
    assert "secret-connection-string" not in str(error.value)


def test_publisher_endpoint_requires_secret_and_health_cannot_publish(
    stores, monkeypatch
):
    client = web.app.test_client()
    monkeypatch.setenv("CRON_SECRET", "scheduler-test-secret")
    with patch.object(web, "Publisher") as make:
        assert client.get(f"/api/publish/{ID}").status_code == 401
        assert client.get(f"/health/{ID}").status_code == 200
        make.assert_not_called()
        make.return_value.run.return_value = {
            "status": "skipped",
            "reason": "cooldown",
            "requests": 0,
        }
        result = client.post(
            f"/api/publish/{ID}",
            headers={"Authorization": "Bearer scheduler-test-secret"},
        )
        assert result.status_code == 200
        assert "no-store" in result.headers["Cache-Control"]


def test_credentials_never_appear_in_publisher_logs(stores, monkeypatch):
    from loguru import logger

    lines = []
    monkeypatch.setenv("MATCHPLAY_API_KEY", "private-api-test-key")
    sink = logger.add(lambda message: lines.append(str(message)))
    try:
        Publisher(stores(), session(response(429, retry="120"))).run(ID)
    finally:
        logger.remove(sink)
    text = "".join(lines)
    assert "private-api-test-key" not in text
    assert "http_status=429" in text
    assert "retry_after=120" in text
    assert "cooldown_deadline=" in text
    assert "request_count=" in text


@pytest.mark.parametrize("value", [None, "nan", "inf", "-2", "bad"])
def test_retry_after_rejects_invalid_values(value):
    assert retry_after_seconds(value, 1000) is None
