"""Read saved snapshots only. Match Play access belongs to publisher.py."""

import os
from copy import deepcopy
from datetime import UTC, datetime
from time import time

import requests
from flask import g, has_request_context
from loguru import logger
from vercel.cache import RuntimeCacheError
from vercel.functions import RuntimeCache

from .snapshot_store import StorageUnavailable, get_store

API_ORIGIN = "https://api.matchplay.events"
API_KEY = os.environ.get("MATCHPLAY_API_KEY")
LIVE_TTL = 25
_cache = {}
_runtime = None
_session = requests.Session()
_session.headers.update({"User-Agent": "ScoreHigh/1.1", "Accept": "application/json"})
if API_KEY is None:
    logger.error("No Matchplay API key")
else:
    logger.info(f"Matchplay API key: {API_KEY}")
    _session.headers["Authorization"] = f"Bearer {API_KEY}"


class ScoreCacheUnavailable(requests.RequestException):
    pass


def _shared():
    """Read-only access to pre-migration snapshots; never used by the publisher."""
    global _runtime
    if not os.environ.get("VERCEL"):
        return None
    if _runtime is None:
        namespace = "scorehigh-live-v2-" + os.environ.get(
            "VERCEL_PROJECT_ID", "default"
        )
        _runtime = RuntimeCache(namespace=namespace, strict=True)
    return _runtime


def _read(key):
    try:
        store = get_store()
        if store.configured:
            value = (
                store.state()
                if key == "rate-state"
                else store.snapshot(int(key.split(":")[1]))
            )
        else:
            legacy = _shared()
            value = legacy.get(key) if legacy else _cache.get(key)
        if value is not None:
            _cache[key] = deepcopy(value)
        return deepcopy(value)
    except StorageUnavailable, RuntimeCacheError, OSError, ValueError, TypeError:
        logger.warning("snapshot_storage_read_failed record={}", key)
        fallback = deepcopy(_cache.get(key))
        if fallback is not None:
            fallback["cache_unavailable"] = True
            return fallback
        raise ScoreCacheUnavailable(
            "Saved scores are temporarily unavailable"
        ) from None


def get_cached_tournament_snapshot(tournament_id):
    # One consistent roster/card pair per request, even during a publication.
    key = f"tournament:{tournament_id}"
    if has_request_context():
        snapshots = g.setdefault("saved_snapshots", {})
        if key not in snapshots:
            try:
                snapshots[key] = _read(key)
            except ScoreCacheUnavailable as error:
                snapshots[key] = error
                raise
        if isinstance(snapshots[key], ScoreCacheUnavailable):
            raise snapshots[key]
        return deepcopy(snapshots[key])
    return _read(key)


def get_tournament_snapshot(tournament_id):
    saved = get_cached_tournament_snapshot(tournament_id)
    if saved is None:
        raise ScoreCacheUnavailable("No published scores for this tournament yet")
    return saved


def get_tournament_data_cached(tournament_id):
    return get_tournament_data(tournament_id)


def get_tournament_data(tournament_id):
    return get_tournament_snapshot(tournament_id)["tournament"]


def get_cards(tournament_id):
    return get_tournament_snapshot(tournament_id)["cards"]


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp else ""


def snapshot_details(tournament_id):
    storage_error = False
    try:
        saved = get_cached_tournament_snapshot(tournament_id)
    except ScoreCacheUnavailable:
        saved = None
        storage_error = True
    try:
        state = _read("rate-state") or {}
    except ScoreCacheUnavailable:
        state = {}
        storage_error = True
    now = time()
    retry_at = max(state.get("cooldown", 0), state.get("budget_retry_at", 0))
    publisher_active = False
    if state.get("in_flight"):
        try:
            store = get_store()
            publisher_active = bool(
                store.configured and store.command("EXISTS", store.key("lease"))
            )
        except StorageUnavailable:
            storage_error = True
    if (
        storage_error
        or state.get("cache_unavailable")
        or (saved and saved.get("cache_unavailable"))
    ):
        health = "cache_unavailable"
    elif now < state.get("cooldown", 0):
        health = state.get("reason", "unavailable")
    elif state.get("in_flight"):
        health = "publishing" if publisher_active else "publisher_interrupted"
    elif state.get("rate_limited"):
        health = "recovery_pending"
    elif not saved:
        health = "waiting"
    elif now - saved["fetched_at"] >= LIVE_TTL:
        health = "cached"
    else:
        health = "ready"
    return {
        "updated_at": _iso(saved["fetched_at"]) if saved else None,
        "state": health,
        "retry_at": _iso(retry_at) if retry_at > now else "",
        "last_successful_update": _iso(saved["fetched_at"]) if saved else None,
        "snapshot_age_seconds": max(0, int(now - saved["fetched_at"]))
        if saved
        else None,
        "publisher_cooldown_until": _iso(state.get("cooldown", 0)),
        "publisher_retry_at": _iso(retry_at),
        "confirmed_rate_limit": bool(state.get("rate_limited")),
        "confirmed_rate_limit_deadline": _iso(state.get("rate_limit_deadline", 0)),
        "limit_scope": state.get("scope", ""),
        "response_status": state.get("status_code"),
        "failure_count": state.get("failures", 0),
        "request_count": state.get("request_count", 0),
        "last_successful_publication": _iso(state.get("last_successful_publication")),
        "publisher_request_unsettled": bool(state.get("in_flight")),
        "publisher_active": publisher_active,
        "storage_configured": get_store().configured,
        "read_only": True,
    }


def snapshot_status(tournament_id):
    details = snapshot_details(tournament_id)
    return details["updated_at"], bool(
        details["updated_at"] and details["state"] != "ready"
    )
