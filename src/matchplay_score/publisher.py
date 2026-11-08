"""The only component allowed to contact Match Play."""

import math
import os
from email.utils import parsedate_to_datetime
from random import uniform
from time import monotonic
from urllib.parse import urlsplit

import requests
from loguru import logger

from .snapshot_store import StorageUnavailable, get_store
from .tournament import UNKNOWN, parse_card, parse_tournament
from .matchplay_api import _session

API_ORIGIN = "https://api.matchplay.events"
PUBLISH_INTERVAL = 25
ROSTER_INTERVAL = 300


class PublisherStopped(Exception):
    def __init__(self, reason):
        self.reason = reason


def retry_after_seconds(value, now):
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            return float(int(value))
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return max(0, date.timestamp() - now)
    except TypeError, ValueError, OverflowError:
        return None


class Publisher:
    def __init__(
        self, store=None, session=None, *, max_requests=None, minute_budget=None
    ):
        self.store = store or get_store()
        self.session = session or _session
        self.session.headers.update(
            {"User-Agent": "ScoreHigh/2.0", "Accept": "application/json"}
        )
        try:
            self.max_requests = int(
                max_requests
                if max_requests is not None
                else os.environ.get("SCOREHIGH_PUBLISH_MAX_REQUESTS", "8")
            )
            self.minute_budget = int(
                minute_budget
                if minute_budget is not None
                else os.environ.get("SCOREHIGH_PUBLISH_REQUESTS_PER_MINUTE", "12")
            )
            if self.max_requests < 1 or self.minute_budget < 1:
                raise ValueError
        except ValueError:
            raise StorageUnavailable(
                "Publisher request budgets must be positive integers"
            ) from None
        self.request_count = 0
        self.deadline = 0
        self.token = ""

    def failure(self, reason, endpoint, status, retry_after, request_id):
        now = self.store.now()
        failures = int(self.store.state().get("failures", 0)) + 1
        supplied = retry_after_seconds(retry_after, now)
        if supplied is not None:
            delay = max(PUBLISH_INTERVAL, supplied)
        else:
            base = 300 if reason == "authentication_error" else 60
            delay = min(900, base * 2 ** min(failures - 1, 5)) * uniform(1, 1.25)
        state = self.store.fail(
            {
                "reason": reason,
                "status_code": status,
                "retry_after": supplied,
                "cooldown": now + math.ceil(delay),
                "endpoint": endpoint,
                "request_id": request_id,
            }
        )
        logger.warning(
            "publisher_failure endpoint={} http_status={} retry_after={} cooldown_deadline={} scope=api_identity failure_count={} request_count={} last_publication={} reason={}",
            endpoint,
            status,
            supplied if supplied is not None else "absent_or_invalid",
            state["cooldown"],
            state["failures"],
            state.get("request_count", 0),
            state.get("last_successful_publication"),
            reason,
        )

    def fetch(self, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.matchplay.events"
            or not parsed.path.startswith("/api/")
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Unexpected pagination origin")
        if self.request_count >= self.max_requests or monotonic() >= self.deadline:
            raise PublisherStopped("invocation_budget")
        request_id, reservation = self.store.reserve(
            self.token, parsed.path, self.minute_budget
        )
        if reservation["status"] != "reserved":
            raise PublisherStopped(reservation["status"])
        self.request_count += 1
        response = None
        try:
            response = self.session.get(url, timeout=(3, 12), allow_redirects=False)
        except requests.Timeout:
            self.failure("timeout", parsed.path, None, None, request_id)
            raise PublisherStopped("timeout") from None
        except requests.RequestException:
            self.failure("network_error", parsed.path, None, None, request_id)
            raise PublisherStopped("network_error") from None
        status = response.status_code
        if status == 429:
            self.failure(
                "rate_limited",
                parsed.path,
                status,
                response.headers.get("Retry-After"),
                request_id,
            )
            raise PublisherStopped("rate_limited")
        if not 200 <= status < 300:
            reason = (
                "authentication_error"
                if status == 401
                else "authorization_error"
                if status == 403
                else "upstream_error"
            )
            self.failure(
                reason,
                parsed.path,
                status,
                response.headers.get("Retry-After"),
                request_id,
            )
            raise PublisherStopped(reason)
        try:
            payload = response.json()
            if not isinstance(payload, dict) or "data" not in payload:
                raise ValueError("Missing response envelope")
        except ValueError:
            self.failure("invalid_response", parsed.path, status, None, request_id)
            raise PublisherStopped("invalid_response") from None
        if not self.store.settle(request_id, status):
            raise PublisherStopped("lease_lost")
        state = self.store.state()
        logger.info(
            "publisher_request endpoint={} http_status={} retry_after=none cooldown_deadline={} request_count={} invocation_requests={} last_publication={}",
            parsed.path,
            status,
            state.get("cooldown", 0),
            state.get("request_count", 0),
            self.request_count,
            state.get("last_successful_publication"),
        )
        return payload

    def cards(self, tournament_id):
        url = f"{API_ORIGIN}/api/tournaments/{tournament_id}/cards"
        items = []
        seen = set()
        while url:
            if url in seen:
                raise ValueError("Repeated pagination link")
            seen.add(url)
            payload = self.fetch(url)
            if (
                not isinstance(payload["data"], list)
                or not isinstance(payload.get("links"), dict)
                or "next" not in payload["links"]
            ):
                raise ValueError("Incomplete card page")
            items.extend(payload["data"])
            url = payload["links"]["next"]
        return items

    def run(self, tournament_id, *, force=False):
        self.request_count = 0
        self.deadline = monotonic() + 45
        try:
            self.token, admission = self.store.acquire(
                tournament_id, interval=PUBLISH_INTERVAL, force=force
            )
            if admission["status"] != "acquired":
                state = admission.get("state", {})
                logger.info(
                    "publisher_skipped tournament={} reason={} cooldown_deadline={} request_count={} last_publication={}",
                    tournament_id,
                    admission["status"],
                    state.get("cooldown", 0),
                    state.get("request_count", 0),
                    state.get("last_successful_publication"),
                )
                return {
                    "status": "skipped",
                    "reason": admission["status"],
                    "requests": 0,
                }
            try:
                saved = self.store.snapshot(tournament_id)
                now = self.store.now()
                refresh_roster = (
                    force
                    or not saved
                    or now - saved.get("roster_fetched_at", 0) >= ROSTER_INTERVAL
                )
                tournament = saved["tournament"] if saved else None
                roster_at = saved.get("roster_fetched_at", 0) if saved else 0
                if refresh_roster:
                    tournament = self.fetch(
                        f"{API_ORIGIN}/api/tournaments/{tournament_id}?includeArenas=true&includePlayers=true"
                    )["data"]
                    roster_at = self.store.now()
                cards = self.cards(tournament_id)
                # New players/arenas invalidate a reused roster, without per-player fetching.
                if not refresh_roster:
                    player_ids = {
                        player["playerId"] for player in tournament["players"]
                    }
                    arena_ids = {arena["arenaId"] for arena in tournament["arenas"]}
                    if any(
                        card["playerId"] not in player_ids
                        or any(
                            game["arenaId"] not in arena_ids
                            for game in card["singlePlayerGames"]
                        )
                        for card in cards
                    ):
                        tournament = self.fetch(
                            f"{API_ORIGIN}/api/tournaments/{tournament_id}?includeArenas=true&includePlayers=true"
                        )["data"]
                        roster_at = self.store.now()
                parsed = parse_tournament(tournament)
                for card in cards:
                    parsed_card = parse_card(card, parsed)
                    if parsed_card.player_name == UNKNOWN:
                        raise ValueError("Roster is missing a card's player")
                    parsed_card.computed_score()
                snapshot = {
                    "tournament": tournament,
                    "cards": cards,
                    "roster_fetched_at": roster_at,
                    "completed": tournament.get("status") == "completed",
                }
                if not self.store.publish(self.token, tournament_id, snapshot):
                    raise PublisherStopped("lease_lost")
                state = self.store.state()
                logger.info(
                    "publisher_published tournament={} request_count={} invocation_requests={} last_publication={}",
                    tournament_id,
                    state.get("request_count", 0),
                    self.request_count,
                    state.get("last_successful_publication"),
                )
                return {"status": "published", "requests": self.request_count}
            except KeyError, TypeError, ValueError, OverflowError:
                self.failure(
                    "invalid_response",
                    f"/api/tournaments/{tournament_id}",
                    None,
                    None,
                    None,
                )
                raise PublisherStopped("invalid_response") from None
            finally:
                self.store.release(self.token)
        except PublisherStopped as error:
            logger.info(
                "publisher_stopped tournament={} reason={} invocation_requests={}",
                tournament_id,
                error.reason,
                self.request_count,
            )
            return {
                "status": "skipped",
                "reason": error.reason,
                "requests": self.request_count,
            }
        except StorageUnavailable:
            logger.error(
                "publisher_storage_failure tournament={} invocation_requests={}; upstream fetching stopped",
                tournament_id,
                self.request_count,
            )
            # A reserved-but-unsettled request remains marked in shared state.
            return {
                "status": "unavailable",
                "reason": "storage_failure",
                "requests": self.request_count,
            }


def main():
    from argparse import ArgumentParser
    import json

    parser = ArgumentParser(description="Publish one complete tournament snapshot")
    parser.add_argument("tournament_id", type=int)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh completed/unchanged events; cooldowns and budgets still apply",
    )
    args = parser.parse_args()
    print(json.dumps(Publisher().run(args.tournament_id, force=args.force)))


if __name__ == "__main__":
    main()
