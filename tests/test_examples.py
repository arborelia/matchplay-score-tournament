from unittest.mock import Mock

import requests
from matchplay_score import web


def test_examples_show_all_history_states_without_api_calls(monkeypatch):
    get = Mock(side_effect=AssertionError("Examples must not call Match Play"))
    monkeypatch.setattr(requests.sessions.Session, "get", get)
    with web.app.test_client() as client:
        response = client.get("/examples/")
        assert response.status_code == 200
        html = response.text
        for label in ["Completed", "Invalid", "Voided", "Card in progress"]:
            assert label in html
        assert "3 Mid games; the limit is 2." in html
        assert "erasing the latest entry" in html
        assert 'data-examples="true"' in html
        assert html.count('class="history-card-heading"') == 4
        assert html.count('class="last-added-badge latest-card-badge"') == 1
        assert html.count('class="last-added-badge"') == 4
        assert 'datetime="2026-11-07T14:00:00Z"' in html
        assert "Added time unavailable" not in html
        for path in [
            "standings/",
            "monitor/",
            "machines/",
            "how-it-works/",
            "player/Alex_Morgan",
        ]:
            assert client.get("/examples/" + path).status_code == 200
    get.assert_not_called()
