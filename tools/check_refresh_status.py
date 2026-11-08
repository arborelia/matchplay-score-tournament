"""Check refresh messages, cooldowns, pause, and recovery without Match Play."""

import logging
from datetime import UTC, datetime
from threading import Thread
from unittest.mock import patch

from browser_helpers import launch_browser
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from matchplay_score import web
from matchplay_score.examples import example_standings


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    health = {
        "updated_at": datetime.now(UTC).isoformat(),
        "state": "ready",
        "retry_at": "",
    }
    server = make_server("127.0.0.1", 0, web.app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (
            patch.object(web, "best_cards", return_value=example_standings()) as reads,
            patch.object(web, "snapshot_details", side_effect=lambda _: dict(health)),
            sync_playwright() as p,
        ):
            browser = launch_browser(p)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.clock.install()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            url = f"http://127.0.0.1:{server.server_port}/ranking/282630"
            page.goto(url)
            status = page.locator("[data-refresh-status]")
            refresh = page.locator("[data-refresh]")
            auto = page.locator("[data-auto]")
            expect(status).to_contain_text("checks every 25s")
            health["state"] = "cached"
            refresh.click()
            expect(status).to_contain_text("Showing saved scores.")
            expect(status).not_to_have_attribute("data-error", "true")

            health["state"] = "rate_limited"
            health["retry_at"] = page.evaluate(
                "new Date(Date.now() + 60000).toISOString()"
            )
            refresh.click()
            expect(status).to_contain_text("Match Play rate limit reached.")
            expect(refresh).to_be_enabled()
            count = reads.call_count
            # Browsers keep reading snapshots during the publisher's cooldown.
            for _ in range(2):
                with page.expect_response(url):
                    page.clock.run_for(25000)
                expect(status).to_contain_text("Match Play rate limit reached.")
            assert reads.call_count > count
            health.update(state="ready", retry_at="")
            with page.expect_response(url):
                page.clock.run_for(25000)
            expect(status).to_contain_text("checks every 25s")
            expect(status).not_to_have_attribute("data-error", "true")

            # A site response can also ask the browser to wait before retrying.
            page.route(
                "**/ranking/282630",
                lambda route: route.fulfill(
                    status=503, headers={"Retry-After": "90"}, body="Unavailable"
                ),
            )
            refresh.click()
            expect(status).to_contain_text("HTTP 503")
            expect(refresh).to_be_disabled()
            page.clock.run_for(89000)
            expect(refresh).to_be_disabled()
            auto.click()
            expect(status).to_contain_text("Automatic checks paused.")
            count = reads.call_count
            page.clock.run_for(120000)
            assert reads.call_count == count
            page.unroute("**/ranking/282630")
            refresh.click()
            expect(status).to_contain_text("checks paused.")
            expect(status).not_to_have_attribute("data-error", "true")

            auto.click()
            page.context.set_offline(True)
            expect(status).to_contain_text("You are offline.")
            count = reads.call_count
            page.clock.run_for(30000)
            assert reads.call_count == count
            with page.expect_response(url):
                page.context.set_offline(False)
                page.clock.run_for(1000)
            expect(status).to_contain_text("checks every 25s")

            previous = page.locator(".ranking-entry").count()
            page.route(
                "**/ranking/282630",
                lambda route: route.fulfill(status=200, body="Incomplete"),
            )
            refresh.click()
            expect(status).to_contain_text("The update was incomplete")
            assert page.locator(".ranking-entry").count() == previous
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        thread.join()
    print(
        "PASS: cache status, rate-limit cooldown, Retry-After, pause, offline recovery, and preserving scores."
    )


if __name__ == "__main__":
    main()
