"""Exercise 25-second page polling during cooldown and with missing snapshots."""

import json
import logging
from threading import Thread
from unittest.mock import patch

import fakeredis
from browser_helpers import launch_browser
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from matchplay_score import matchplay_api as api, web
from matchplay_score.snapshot_store import RedisStore


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(transport=redis.execute_command, identity="browser-check")
    saved = {
        "fetched_at": store.now() - 3600,
        "tournament": {
            "players": [{"playerId": 1, "name": "Saved Player"}],
            "arenas": [
                {"arenaId": 1, "name": "Stars", "tournamentArena": {"status": "active"}}
            ],
        },
        "cards": [
            {
                "cardId": 1,
                "playerId": 1,
                "singlePlayerGames": [{"arenaId": 1, "playerId": 1, "score": 1234}],
            }
        ],
    }
    server = make_server("127.0.0.1", 0, web.app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (
            patch.object(api, "get_store", return_value=store),
            patch.object(
                web,
                "Publisher",
                side_effect=AssertionError("Browser started publisher"),
            ),
            patch.object(
                api._session,
                "get",
                side_effect=AssertionError("Browser contacted Match Play"),
            ) as upstream,
            sync_playwright() as p,
        ):
            browser = launch_browser(p)
            for present in (True, False):
                redis.flushdb()
                api._cache.clear()
                if present:
                    for tournament in (281672, 282316):
                        redis.set(
                            store.key(f"tournament:{tournament}"), json.dumps(saved)
                        )
                redis.set(
                    store.key("publisher"),
                    json.dumps(
                        {
                            "cooldown": store.now() + 600,
                            "reason": "rate_limited",
                            "rate_limited": True,
                            "status_code": 429,
                            "failures": 1,
                            "scope": "api_identity",
                        }
                    ),
                )
                for path in (
                    "/ranking/281672",
                    "/monitor/281672",
                    "/player/281672/Saved_Player",
                    "/machines/281672",
                    "/overlay/281672",
                    "/",
                ):
                    page = browser.new_page(viewport={"width": 1600, "height": 900})
                    page.clock.install()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    # A slow fetch must not cause a second concurrent poll.
                    page.add_init_script("""(() => {
                      const original = window.fetch;
                      window.pollActive = 0; window.pollMaximum = 0;
                      window.fetch = async (...args) => {
                        window.pollActive++;
                        window.pollMaximum = Math.max(window.pollMaximum, window.pollActive);
                        try { return await original(...args); }
                        finally { window.pollActive--; }
                      };
                    })();""")
                    url = f"http://127.0.0.1:{server.server_port}{path}"
                    response = page.goto(url)
                    assert response.status == (200 if present else 503), (
                        path,
                        response.status,
                    )
                    for _ in range(2):
                        with page.expect_response(url):
                            page.clock.run_for(25000)
                        page.wait_for_function("window.pollActive === 0")
                    assert page.evaluate("window.pollMaximum") == 1
                    assert not errors, (path, errors)
                    page.close()
                upstream.assert_not_called()
                if present:
                    assert store.snapshot(281672)["fetched_at"] == saved["fetched_at"]
            browser.close()
    finally:
        server.shutdown()
        thread.join()
    print(
        "PASS: standings, player, machines, monitor, OBS and tournament home poll every 25s with zero Match Play/publisher calls, including empty storage and cooldown."
    )


if __name__ == "__main__":
    main()
