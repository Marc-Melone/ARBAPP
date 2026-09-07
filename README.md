# Event Market Scanner — Kalshi × Polymarket

Collects and compares event-market data from the **Kalshi** and **Polymarket**
public APIs. Built early 2026 as exploratory work on prediction-market structure.

## What it does

**`event_market_scanner.py`** — collects the top events by liquidity from both
venues, including prices for every outcome and event-level liquidity.

```bash
python event_market_scanner.py                 # both platforms
python event_market_scanner.py --kalshi        # Kalshi only
python event_market_scanner.py --limit 100     # top 100 instead of 1000
python event_market_scanner.py --export data.csv
python event_market_scanner.py --top 20        # rich terminal table
```

Endpoints used, both public and unauthenticated:

- Kalshi — `https://api.elections.kalshi.com/trade-api/v2`
- Polymarket — `https://gamma-api.polymarket.com`

**`web_app.py`** — a Flask front end over the collectors that attempts to *match*
equivalent markets across the two venues. Matching is the hard part: the same
real-world event is titled differently on each platform, so the scorer applies

- date canonicalisation (`Sept 30` / `9/30/26` / `2026-09-30` → one form),
- number and magnitude normalisation,
- synonym and proper-name normalisation,
- negation detection, so "will not" does not match "will",
- a combined score blending token-set Jaccard with fuzzy string similarity.

**`event_market_notebook.ipynb`** — exploratory analysis over the collected data:
summary tables per venue, combined dataset shape, and matching experiments.
Outputs are stripped from the committed copy; run it to regenerate them.

## Running

```bash
pip install -r requirements.txt
python event_market_scanner.py --top 20
python web_app.py                      # or: gunicorn web_app:app
```

## Scope and limitations

- Read-only. Collects and compares public market data; places no orders and
  holds no API credentials.
- Cross-venue matching is heuristic. It surfaces *candidate* pairs for review and
  will both miss genuine matches and propose false ones.
- Price differences shown between venues are **not** tradeable arbitrage: fees,
  order-book depth, settlement-source differences and resolution-criteria
  differences are not modelled here.
- Exploratory project, not a maintained tool.

## Related

[`kxsurv`](https://github.com/Marc-Melone/kxsurv) — a later, more rigorous
project on the same venue: a market-surveillance program implementing five
abuse-detection controls over Kalshi's public data, mapped to CFTC DCM Core
Principles.
