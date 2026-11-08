import pytest
import fakeredis

from matchplay_score import matchplay_api as api
from matchplay_score import publisher, snapshot_store
from matchplay_score.snapshot_store import RedisStore


@pytest.fixture(autouse=True)
def isolated_live_cache(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr(api, "_cache", {})
    monkeypatch.setattr(api, "_runtime", None)
    for name in (
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
        "KV_REST_API_URL",
        "KV_REST_API_TOKEN",
        "MATCHPLAY_API_KEY",
        "CRON_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def stores(monkeypatch):
    server = fakeredis.FakeServer()

    def factory(identity="test"):
        client = fakeredis.FakeRedis(server=server, decode_responses=True)
        return RedisStore(identity=identity, transport=client.execute_command)

    monkeypatch.setattr(api, "get_store", factory)
    monkeypatch.setattr(publisher, "get_store", factory)
    monkeypatch.setattr(snapshot_store, "get_store", factory)
    return factory
