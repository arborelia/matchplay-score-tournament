"""Durable snapshots and atomic publisher coordination over Redis REST."""

import json
import os
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import uuid4

import requests


class StorageUnavailable(requests.RequestException):
    pass


ACQUIRE = """
local now = tonumber(redis.call('TIME')[1])
local state = cjson.decode(redis.call('GET', KEYS[2]) or '{}')
if tonumber(state.cooldown or 0) > now then return cjson.encode({status='cooldown', state=state}) end
if tonumber(state.budget_retry_at or 0) > now then return cjson.encode({status='budget', state=state}) end
if redis.call('GET', KEYS[1]) then return cjson.encode({status='busy', state=state}) end
if state.in_flight and state.in_flight ~= cjson.null then return cjson.encode({status='interrupted', state=state}) end
local saved = cjson.decode(redis.call('GET', KEYS[3]) or '{}')
if ARGV[4] ~= '1' then
  if saved.completed then return cjson.encode({status='completed', state=state}) end
  if saved.fetched_at and now - tonumber(saved.fetched_at) < tonumber(ARGV[3]) then
    return cjson.encode({status='fresh', state=state})
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX')
return cjson.encode({status='acquired', state=state})
"""

RESERVE = """
local now = tonumber(redis.call('TIME')[1])
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return cjson.encode({status='lease_lost'}) end
local state = cjson.decode(redis.call('GET', KEYS[2]) or '{}')
if tonumber(state.cooldown or 0) > now then return cjson.encode({status='cooldown'}) end
if state.in_flight and state.in_flight ~= cjson.null then return cjson.encode({status='interrupted'}) end
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now - 60)
local count = redis.call('ZCARD', KEYS[3])
if count >= tonumber(ARGV[3]) then
  local oldest = redis.call('ZRANGE', KEYS[3], 0, 0, 'WITHSCORES')
  state.budget_retry_at = tonumber(oldest[2]) + 60
  redis.call('SET', KEYS[2], cjson.encode(state))
  return cjson.encode({status='budget', retry_at=state.budget_retry_at})
end
redis.call('ZADD', KEYS[3], now, ARGV[4])
redis.call('EXPIRE', KEYS[3], 120)
redis.call('EXPIRE', KEYS[1], ARGV[2])
state.request_count = tonumber(state.request_count or 0) + 1
state.last_request_at = now
state.last_endpoint = ARGV[5]
state.budget_retry_at = 0
state.in_flight = ARGV[4]
redis.call('SET', KEYS[2], cjson.encode(state))
return cjson.encode({status='reserved', now=now, request_count=state.request_count})
"""

SETTLE = """
local state = cjson.decode(redis.call('GET', KEYS[1]) or '{}')
if state.in_flight ~= ARGV[1] then return 0 end
state.in_flight = cjson.null
state.last_http_status = tonumber(ARGV[2])
redis.call('SET', KEYS[1], cjson.encode(state))
return 1
"""

FAILURE = """
local state = cjson.decode(redis.call('GET', KEYS[1]) or '{}')
local failure = cjson.decode(ARGV[1])
state.failures = tonumber(state.failures or 0) + 1
state.cooldown = math.max(tonumber(state.cooldown or 0), tonumber(failure.cooldown))
state.reason = failure.reason
state.status_code = failure.status_code
state.last_http_status = failure.status_code
state.retry_after = failure.retry_after
state.scope = 'api_identity'
state.last_endpoint = failure.endpoint
if state.in_flight == failure.request_id then state.in_flight = cjson.null end
if failure.status_code == 429 then
  state.rate_limited = true
  state.rate_limit_deadline = math.max(tonumber(state.rate_limit_deadline or 0), tonumber(failure.cooldown))
  state.rate_limit_status = 429
end
redis.call('SET', KEYS[1], cjson.encode(state))
return cjson.encode(state)
"""

PUBLISH = """
local now = tonumber(redis.call('TIME')[1])
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local state = cjson.decode(redis.call('GET', KEYS[2]) or '{}')
if tonumber(state.cooldown or 0) > now or (state.in_flight and state.in_flight ~= cjson.null) then return 0 end
local snapshot = cjson.decode(ARGV[2])
snapshot.fetched_at = now
redis.call('SET', KEYS[3], cjson.encode(snapshot))
state.last_successful_publication = now
state.cooldown = 0
state.failures = 0
state.reason = ''
state.status_code = cjson.null
state.rate_limited = false
state.rate_limit_deadline = 0
state.rate_limit_status = cjson.null
state.retry_after = cjson.null
state.scope = ''
redis.call('SET', KEYS[2], cjson.encode(state))
return 1
"""

RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class RedisStore:
    lease_seconds = 120

    def __init__(self, url=None, token=None, identity=None, transport=None):
        self.url = (
            url
            or os.environ.get("UPSTASH_REDIS_REST_URL")
            or os.environ.get("KV_REST_API_URL")
        )
        self.token = (
            token
            or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
            or os.environ.get("KV_REST_API_TOKEN")
        )
        identity = identity or os.environ.get(
            "MATCHPLAY_API_IDENTITY", "shared-matchplay"
        )
        # This deliberately shares coordination across projects using the same identity.
        self.prefix = "scorehigh:{" + sha256(identity.encode()).hexdigest()[:24] + "}"
        self.transport = transport

    @property
    def configured(self):
        return bool(self.transport or (self.url and self.token))

    def command(self, *parts):
        try:
            if self.transport:
                return self.transport(*parts)
            if not self.configured:
                raise StorageUnavailable("Shared snapshot storage is not configured")
            parsed = urlsplit(self.url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
            ):
                raise StorageUnavailable("Invalid shared storage URL")
            response = requests.post(
                self.url,
                json=list(parts),
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=(3, 5),
            )
            response.raise_for_status()
            result = response.json()
            if "error" in result or "result" not in result:
                raise StorageUnavailable("Shared storage rejected the operation")
            return result["result"]
        except StorageUnavailable:
            raise
        except Exception as error:
            # Provider exception text may include connection credentials; never forward it.
            raise StorageUnavailable(
                f"Shared storage operation failed ({type(error).__name__})"
            ) from None

    def key(self, name):
        return f"{self.prefix}:{name}"

    def read(self, name):
        value = self.command("GET", self.key(name))
        if value is None:
            return None
        try:
            record = json.loads(value)
            if not isinstance(record, dict):
                raise ValueError("Invalid record type")
            return record
        except TypeError, ValueError:
            raise StorageUnavailable("Invalid saved storage record") from None

    def snapshot(self, tournament_id):
        return self.read(f"tournament:{tournament_id}")

    def state(self):
        return self.read("publisher") or {}

    def now(self):
        return int(self.command("TIME")[0])

    def script(self, script, keys, *args):
        value = self.command(
            "EVAL", script, len(keys), *[self.key(key) for key in keys], *args
        )
        return json.loads(value) if isinstance(value, (str, bytes)) else value

    def acquire(self, tournament_id, *, interval=25, force=False):
        token = uuid4().hex
        result = self.script(
            ACQUIRE,
            ["lease", "publisher", f"tournament:{tournament_id}"],
            token,
            self.lease_seconds,
            interval,
            "1" if force else "0",
        )
        return token, result

    def reserve(self, token, endpoint, budget):
        request_id = uuid4().hex
        result = self.script(
            RESERVE,
            ["lease", "publisher", "requests"],
            token,
            self.lease_seconds,
            budget,
            request_id,
            endpoint,
        )
        return request_id, result

    def settle(self, request_id, status):
        return self.script(SETTLE, ["publisher"], request_id, status)

    def fail(self, failure):
        return self.script(FAILURE, ["publisher"], json.dumps(failure))

    def publish(self, token, tournament_id, snapshot):
        return self.script(
            PUBLISH,
            ["lease", "publisher", f"tournament:{tournament_id}"],
            token,
            json.dumps(snapshot),
        )

    def release(self, token):
        return self.script(RELEASE, ["lease"], token)


def get_store():
    return RedisStore()
