# ScoreHigh

Scoring system for the TPC ScoreHigh format, as used in [Score High for Trans Lives](https://posts.arborelia.net/trans-lives/).

[Public site](https://score.arborelia.net) | [Preview](https://ashodinventeal.github.io/scorehigh-preview/)

This frontend runs with the existing Flask application, including the hosted
deployment at https://tpc-high-score.vercel.app. The links above are the separate
ScoreHigh deployment and simulated preview.

ScoreHigh multiplies each machine score by its configured multiplier and ranks each player by their best valid card. A card allows two games from each of three eras. Cards with fewer than six games show their progress and unused slots; cards over an era limit stay in player history but do not count toward standings.

## Run locally

Python 3.14 and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --group dev
uv run scorehigh --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. The older `matchplay-score` command remains available.

Configure tournament IDs, names, and Match Play links in `src/matchplay_score/web.py`. Machine eras and multipliers live in `src/matchplay_score/tournament.py`. The configured test event is tournament `281672`. Alternatively, you could attach this to your already built back-end. Ideally you're using the python scripts in general so you're not rewriting the wheel here.

## Standings and event display

The standings support search, expandable machine scores, and player history. Claim and follow selections are stored on the viewer's device; they do not create accounts or change tournament records.

The Monitor button opens the event display. Page one shows prominent cards for ranks 1-3 and rows for ranks 4-8. Later pages show the remaining players, with capacity adjusted to the available height. Pages rotate every 12 seconds with a whole-board fade; manual paging, pause, fullscreen, and reduced motion are supported. Common supported landscape sizes are from 1024x768 through 2560x1440, including 1600x900 and 1920x1080.

Display settings provide light/dark/system colors, a simple layout, Classic Windows, and a 2000s Portal layout. Hey look, I wanted options! Standings scores default to readable monospace; the event display uses 7 Segment Display font. The optional score fonts are bundled locally with their license notices.

## Score snapshots and caching

Browsers check saved snapshots every 25 seconds. Pages never contact Match Play,
publish snapshots, or paginate, including when storage is empty or data is old.
A separate authenticated publisher fetches once on behalf of all visitors.

Redis stores complete snapshots, a shared publisher lease, request budgets and
cooldown state. A failed update keeps the last complete snapshot and its original
timestamp. Health information is available at `/health/<tournament_id>`.

Configure shared storage and a trusted publisher schedule before deploying.
[Publisher setup and verification](PUBLISHER.md) covers the required settings,
rate-limit handling, migration, diagnostics and tests. The static GitHub Pages
exporter reads saved data too; it does not refresh Match Play.

Player URLs use underscores for spaces. Player histories show Voided only when
Match Play flags the card as voided, and exclude it from standings. Starting a
new card does not void an earlier incomplete card. Completed cards over a category
limit are greyed out with the reason and scorekeeper correction instructions;
the site never erases scores itself. Monitor ranking rows show each machine's
raw score beneath its tile.

Monitor Settings includes an optional Flyer backgrounds checkbox, off by default.
When enabled, each game row in the top three expanded cards shows its matching
promotional flyer at 10% opacity. Images are bundled locally and are requested
only when the setting is enabled on the monitor. Source links are retained in
`static/images/flyers/sources.json`. This setting does not request Match Play data.

The `/examples/` page shows simulated valid, invalid, voided, and in-progress
cards. `/examples/standings/` and `/examples/monitor/` show 25 simulated players.
These pages make no Match Play requests and do not change tournament records.

Here's how to build the public snapshot locally:

```sh
uv run python -m matchplay_score.export --output _site --base-path /scorehigh
```

This command uses real tournament data so be sure it is connected to where you are running the Matchplay tournament. The separate preview uses clearly labeled simulated data:

```sh
uv run python tools/build_tournament_demo.py --output .preview/demo/index.html
```

## Project structure

- `src/matchplay_score/`: scoring, cached API reads, Flask routes, and static export.
- `templates/components.html`: shared game banners and card progress labels.
- `static/matchplay.css`: simple standings and shared banners.
- `static/monitor.css` and `static/monitor.js`: event display layout and paging.
- `static/scorehigh.js`: display preferences, player tracking, disclosures, and refresh.
- `tests/`: offline scoring, rendering, export, and API-cache checks.
- `tools/`: demo export and browser checks.

## Checks and packages

```sh
uv run pytest
uv run ruff check src tests tools
uv build
```

The default tests run offline. To run the public API integration check:

```sh
uv run pytest -m integration
```

For browser checks, install Chromium once, then point the checker at the demo export or a running site:

```sh
uv run --with playwright playwright install chromium
uv run --with playwright python tools/check_ui.py --preview-dir .preview/demo
uv run --with playwright python tools/check_monitor_responsive.py --preview-dir .preview/demo
```

## Attribution and licensing

Original scoring code: Elia Lake / arborelia, copyright 2026. Original terms are retained unchanged in `LICENSE.txt`. Front-end Code and Design: Ash Harmeyer / Ashodin, copyright 2026.

Font notices are included in `src/matchplay_score/static/fonts/`. The small comma image preserves the outline from 7 Segment Display by Bernard Teo, based on Mylicia's segment font, under the included CC BY-SA 3.0 terms. Other font families retain their own notices. This repository does not relicense those assets.
