# Snapshot publisher

Pages never use Match Play as their backend. Standings, player pages, machines,
monitor displays, and OBS read complete saved snapshots. Browser checks default
to 25 seconds. Missing or old data cannot start a publisher or paginate the API.

## Configure before deploying

Connect an existing Upstash Redis database, or provision one in the project's
Vercel Marketplace. Use durable storage with eviction disabled. Snapshots and
publisher state have no expiry; only leases and rolling budget entries expire.
Do not use a disposable test database for a tournament.

Set these in the Vercel project's environment settings:

| Variable | Purpose |
| --- | --- |
| `UPSTASH_REDIS_REST_URL` | Redis REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | Server-side credential with GET, SET, TIME and EVAL access |
| `CRON_SECRET` | Secret accepted by the publisher endpoint |
| `MATCHPLAY_API_IDENTITY` | Optional stable API-account label shared by all publishers using that identity; defaults to `shared-matchplay` |
| `SCOREHIGH_PUBLISH_MAX_REQUESTS` | Optional maximum upstream requests in one run; default 8 |
| `SCOREHIGH_PUBLISH_REQUESTS_PER_MINUTE` | Optional shared rolling-minute request budget; default 12 |

`KV_REST_API_URL` and `KV_REST_API_TOKEN` are supported as aliases for the Redis
connection settings. Existing Match Play API-key configuration is unchanged.
Never put publisher or storage credentials in a browser URL.

All instances sharing an API identity must use the same database and identity
label, including separate Vercel projects. Leave the default shared label when
the upstream limit's scope is unknown. Request budgets are local safety limits,
not claims about Match Play's endpoint limits. Do not enable Redis eviction:
losing publisher state could lose a cooldown. See the provider's
[eviction documentation](https://upstash.com/docs/redis/features/eviction).

Without Redis, existing Vercel Runtime Cache snapshots can still be read during
migration. The publisher refuses to contact Match Play without shared storage.
Configure the database, publish an initial snapshot, then verify the read-only
health endpoint before switching traffic to this release.

## Run the publisher

Use a trusted scheduler to invoke `POST /api/publish/282316` with the header
`Authorization: Bearer <CRON_SECRET>`. GET is also accepted for authenticated
Vercel cron invocations. Do not expose this header in page JavaScript.

For a one-off publication with the environment configured:

```sh
uv run python -m matchplay_score.publisher 282316
```

The scheduler is separate from browser checks. Invoke once per 25–60 seconds
while the tournament is active, within the hosting plan's scheduling limits.
Vercel's legacy cron service allows daily runs on Hobby and minute-level runs on
Pro; choose an appropriate trusted scheduler rather than using visitor requests
as a scheduling mechanism. See
[Vercel cron limits](https://vercel.com/docs/schedules/cron-jobs/usage-and-pricing).
This repository does not silently create a scheduler or a database.
Disable scheduler catch-up/backfill runs; a skipped tick should stay skipped.

One atomic, API-identity-wide lease admits a publisher. Other invocations return
immediately; they do not queue or sleep. Every upstream request reserves budget
and checks the persisted cooldown again. The publisher stages all card pages,
validates them, then writes one complete snapshot. A failed page or exhausted
budget leaves the previous snapshot and its timestamp intact. Increase the
per-run budget if a tournament requires more card pages than the configured cap,
and check the shared minute budget as well.

Roster data is reused for five minutes, with an earlier refresh when cards refer
to newly added players or arenas. Completed tournaments stop automatic updates.
`--force` (or authenticated `?force=1`) permits a later correction refresh;
cooldowns, locks and request budgets still apply.

## Cooldowns and diagnostics

Only an HTTP 429 confirms a rate limit. Retry-After accepts seconds or an HTTP
date. A valid longer delay is never shortened to the local backoff cap. Missing
or invalid values use exponential backoff with jitter. Authentication failures,
access denials, timeouts, malformed responses and storage failures are recorded
separately. No undocumented remaining/reset headers are used.

Cooldowns and failure counts persist across workers and restarts. Expiry allows
one coordinated attempt; it does not clear the confirmed-rate-limit flag. A new
429 extends the cooldown. Only a complete successful publication clears resolved
rate-limit state. No requests are queued for later catch-up.

`GET /health/282316` is public, read-only, and never calls Match Play. It reports
the last update, snapshot age, cooldown/retry deadline, confirmed rate-limit
state, request count and failure count. Vercel runtime logs identify the endpoint,
HTTP status, parsed Retry-After, cooldown deadline and last successful publication.
New publisher diagnostics never include authorization headers or response bodies.

If storage fails after a request was reserved, its outcome may be uncertain.
An unsettled request deliberately blocks subsequent publishers even after the
lease expires. Review the runtime logs and upstream cooldown before repairing
that publisher record in Redis; do not blindly delete cooldown state. This also
prevents a worker crash from silently dropping a 429 it could not persist.

## Verify

```sh
uv run pytest -q
uv run --with playwright python tools/check_snapshot_polling.py
uv run --with playwright python tools/check_overlay.py
uv run --with playwright python tools/check_refresh_status.py
```

Tests execute the Redis Lua scripts using independent clients sharing a Redis
test server. They cover restarted workers, concurrent publishers, second 429s,
long Retry-After values, missing storage, incomplete pagination, credential-safe
diagnostics, and zero upstream requests from browser/health paths.
