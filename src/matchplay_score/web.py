from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import version
from math import ceil
from random import Random
from hmac import compare_digest
import os

import requests
from flask import Flask, abort, g, render_template, request

from .branding import REPOSITORY_DESCRIPTION, SITE_DESCRIPTION, SITE_TITLE
from .broadcast import overlay_entries, overlay_options, tournament_cards
from .examples import register_examples
from .flyers import FLYER_IMAGES
from .matchplay_api import (
    ScoreCacheUnavailable,
    get_tournament_data_cached,
    snapshot_details,
)
from .ranking import best_cards, player_cards
from .publisher import Publisher
from .snapshot_store import StorageUnavailable
from .tournament import (
    ARENAS,
    CARD_GAME_LIMIT,
    MAX_PER_ERA,
    Card,
    SinglePlayerGame,
    parse_tournament,
)

app = Flask(__name__)
register_examples(app)
APP_VERSION = version("matchplay-score") + "-publisher-snapshots-1"
TOURNAMENT_INFO = {
    281672: "Test Tournament",
    282316: "Score High for Trans Lives",
    282630: "ScoreHigh test run",
}
TOURNAMENT_EMOJI = (
    "🏅",
    "🎖️",
    "🏁",
    "🎯",
    "🎳",
    "🕹️",
    "🎮",
    "🎲",
    "🎪",
    "🎟️",
    "🎰",
    "🏟️",
    "🎊",
    "🎉",
    "🎱",
)


def tournament_matchplay_urls():
    return {
        id: f"https://app.matchplay.events/tournaments/{id}/" for id in TOURNAMENT_INFO
    }


def tournament_icons():
    """Give listed tournaments repeatable random icons, avoiding duplicates."""
    icons = {}
    for tournament_id in sorted(TOURNAMENT_INFO):
        choices = Random(tournament_id).sample(TOURNAMENT_EMOJI, len(TOURNAMENT_EMOJI))
        icons[tournament_id] = next(
            (icon for icon in choices if icon not in icons.values()), choices[0]
        )
    if 282316 in TOURNAMENT_INFO:
        icons[282316] = "🏆"
    return icons


@app.context_processor
def site_context():
    return {
        "tournaments": TOURNAMENT_INFO,
        "max_per_era": MAX_PER_ERA,
        "card_game_limit": CARD_GAME_LIMIT,
        "asset_version": APP_VERSION,
        "site_title": SITE_TITLE,
        "site_description": SITE_DESCRIPTION,
        "repository_description": REPOSITORY_DESCRIPTION,
        "tournament_icons": tournament_icons(),
        "matchplay_urls": tournament_matchplay_urls(),
        "static_snapshot": bool(app.config.get("SNAPSHOT_AT")),
        "cached_scores": getattr(g, "cached_scores", False),
        "score_health": getattr(g, "score_health", {}),
        "flyer_images": FLYER_IMAGES,
    }


def score_timestamp(tournament_id):
    health = snapshot_details(tournament_id)
    g.score_health = health
    g.cached_scores = bool(health["updated_at"] and health["state"] != "ready")
    return health["updated_at"] or app.config.get("SNAPSHOT_AT") or ""


@app.before_request
def known_tournament():
    tournament_id = (request.view_args or {}).get("tournament_id")
    if tournament_id is not None and tournament_id not in TOURNAMENT_INFO:
        abort(404)


@app.after_request
def cache_public_pages(response):
    if request.endpoint in {"render_publisher", "render_health"}:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vercel-CDN-Cache-Control"] = "no-store"
        return response
    if request.method in {"GET", "HEAD"} and request.endpoint != "static":
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
            response.headers["Vercel-CDN-Cache-Control"] = (
                "public, s-maxage=5, stale-if-error=3600"
            )
        else:
            response.headers["Cache-Control"] = "no-store"
    return response


def tournament_title(tournament_id):
    return TOURNAMENT_INFO.get(tournament_id, f"Tournament {tournament_id}")


@app.template_filter("commafy")
def commafy(value: int) -> str:
    "Apply thousands separator to integers"
    return f"{value:,d}"


@app.template_filter("card_key")
def card_key(card) -> str:
    return sha256(repr(card).encode("utf-8")).hexdigest()[:16]


@app.template_filter("added_time")
def added_time(value):
    try:
        added = datetime.fromisoformat(value)
        if added.tzinfo is None:
            added = added.replace(tzinfo=UTC)
        added = added.astimezone(UTC)
        return f"{added:%b} {added.day}, {added:%Y %I:%M:%S %p} UTC"
    except TypeError, ValueError:
        return "Time unavailable"


@app.route("/ranking/<int:tournament_id>")
def render_ranking(tournament_id: int):
    return standings_page(tournament_id)


@app.route("/monitor/<int:tournament_id>")
def render_monitor(tournament_id: int):
    return standings_page(tournament_id, monitor=True)


@app.route("/obs/<int:tournament_id>")
def render_obs(tournament_id: int):
    # Configuration itself needs no Match Play data; only the preview loads cards.
    return render_template(
        "obs.html",
        title=tournament_title(tournament_id),
        tournament_id=tournament_id,
        active_tab="obs",
    )


@app.route("/overlay/<int:tournament_id>")
def render_overlay(tournament_id: int):
    options = overlay_options()
    try:
        cards = tournament_cards(tournament_id)
    except requests.RequestException as error:
        app.logger.warning(
            "OBS snapshot unavailable: tournament=%s error=%s reason=%s; no Match Play request made",
            tournament_id,
            type(error).__name__,
            str(error) or "No saved scores available",
        )
        # Even a cold-start outage must leave OBS with a transparent, retrying view.
        return render_template(
            "overlay.html",
            title=tournament_title(tournament_id),
            entries=[],
            options=options,
            cached_scores=True,
        ), 503
    return render_template(
        "overlay.html",
        title=tournament_title(tournament_id),
        entries=overlay_entries(cards, options["mode"], options["count"]),
        options=options,
        updated_at=score_timestamp(tournament_id),
    )


def standings_page(tournament_id: int, *, monitor: bool = False):
    missing = False
    try:
        cards = best_cards(tournament_id, use_cache=True, include_invalid=not monitor)
    except ScoreCacheUnavailable:
        cards = []
        missing = True
    title = tournament_title(tournament_id)
    html = render_template(
        "ranking.html",
        cards=cards,
        title=title,
        tournament_id=tournament_id,
        updated_at=score_timestamp(tournament_id),
        active_tab="standings",
        monitor=monitor,
    )
    return saved_page_response(html, missing)


def saved_page_response(html, missing):
    if not missing:
        return html
    reason = getattr(g, "score_health", {}).get("state", "waiting")
    return html, 503, {"X-ScoreHigh-Error": reason}


@app.route("/health/<int:tournament_id>")
def render_health(tournament_id):
    return snapshot_details(tournament_id)


@app.route("/api/publish/<int:tournament_id>", methods=["GET", "POST"])
def render_publisher(tournament_id):
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        return {"error": "Publisher authentication is not configured"}, 503
    if not compare_digest(request.headers.get("Authorization", ""), f"Bearer {secret}"):
        return {"error": "Unauthorized"}, 401
    try:
        result = Publisher().run(tournament_id, force=request.args.get("force") == "1")
    except StorageUnavailable:
        return {
            "error": "Publisher storage or budget configuration is unavailable"
        }, 503
    return result, 503 if result["status"] == "unavailable" else 200


@app.route("/player/<int:tournament_id>/<path:player_name>")
def render_player(tournament_id: int, player_name: str):
    missing = False
    try:
        cards = player_cards(tournament_id, player_name)
    except ScoreCacheUnavailable:
        cards = []
        missing = True
    if not missing and not cards and "_" in player_name:
        player_name = player_name.replace("_", " ")
        cards = player_cards(tournament_id, player_name)
    if not cards and not missing:
        abort(404)
    player_name = cards[0].player_name if cards else player_name.replace("_", " ")
    valid_cards = [card for card in cards if card.is_valid()]
    standings = best_cards(tournament_id, use_cache=True) if not missing else []
    rank = next(
        (i for i, card in enumerate(standings, 1) if card.player_name == player_name),
        None,
    )
    html = render_template(
        "player.html",
        cards=cards,
        player_name=player_name,
        best_card=max(valid_cards, key=Card.computed_score) if valid_cards else None,
        rank=rank,
        title=tournament_title(tournament_id),
        tournament_id=tournament_id,
        updated_at=score_timestamp(tournament_id),
        active_tab="standings",
    )
    return saved_page_response(html, missing)


@app.route("/machines/<int:tournament_id>")
def render_machines(tournament_id: int):
    missing = False
    try:
        arenas = parse_tournament(
            get_tournament_data_cached(tournament_id)
        ).arenas_by_id.values()
    except ScoreCacheUnavailable:
        arenas = []
        missing = True
    html = render_template(
        "machines.html",
        arenas=arenas,
        title=tournament_title(tournament_id),
        tournament_id=tournament_id,
        active_tab="machines",
        updated_at=score_timestamp(tournament_id),
    )
    return saved_page_response(html, missing)


@app.route("/how-it-works/<int:tournament_id>")
def render_how_it_works(tournament_id: int):
    arenas = {arena.short_name: arena for arena in ARENAS}
    scores = {
        "Stars": 280_000,
        "Quicksilver": 340_000,
        "Creature": 120_000_000,
        "Theatre": 700_000_000,
        "Iron Maiden": 360_000_000,
        "Jurassic Park": 120_000_000,
    }
    games = [
        SinglePlayerGame(
            i, name, "Example", score, arenas[name].multiplier, arenas[name].era
        )
        for i, (name, score) in enumerate(scores.items())
    ]
    example = Card("Example", games)
    lower = Card("Example", [replace(games[0], score=100_000), *games[1:]])
    tron = arenas["TRON"]
    invalid = Card(
        "Example",
        [
            *games,
            SinglePlayerGame(
                99, tron.short_name, "Example", 25_000_000, tron.multiplier, tron.era
            ),
        ],
    )
    return render_template(
        "how_it_works.html",
        title=tournament_title(tournament_id),
        tournament_id=tournament_id,
        active_tab="how_it_works",
        example=example,
        lower=lower,
        invalid=invalid,
    )


@app.errorhandler(requests.RequestException)
def upstream_error(error):
    app.logger.error(
        "Tournament data unavailable: path=%s; error=%s",
        request.path,
        type(error).__name__,
        exc_info=(type(error), error, error.__traceback__),
    )
    health = snapshot_details((request.view_args or {}).get("tournament_id", 0))
    reason = (
        "cache_unavailable"
        if isinstance(error, ScoreCacheUnavailable)
        else "upstream_error"
    )
    if health["state"] not in {"ready", "cached", "waiting"}:
        reason = health["state"]
    headers = {"X-ScoreHigh-Error": reason}
    if health["retry_at"]:
        retry = datetime.fromisoformat(health["retry_at"]) - datetime.now(UTC)
        headers["Retry-After"] = str(max(1, ceil(retry.total_seconds())))
    return (
        render_template(
            "error.html",
            title="Scores unavailable",
            message="We couldn't reach Match Play. Please try again in a moment.",
        ),
        503,
        headers,
    )


@app.errorhandler(404)
def not_found(error):
    return (
        render_template(
            "error.html",
            title="Page not found",
            message="This player or page couldn't be found. Choose a tournament to continue.",
        ),
        404,
    )


# temporarily moved to /index
@app.route("/index")
def index():
    return render_template(
        "index.html",
        tournaments=TOURNAMENT_INFO,
    )


@app.route("/")
def front_page_redirect():
    return standings_page(282316)
