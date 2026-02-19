#!/usr/bin/env python3
"""
Event Market Scanner — Web App (Render-ready)
==============================================
Flask web app that fetches prediction market data from Polymarket & Kalshi,
detects cross-platform arbitrage, and displays results in a clean UI.

Usage (local):
    python web_app.py
    python web_app.py --port 8080

Render start command:
    gunicorn web_app:app
"""

import os
import re
import calendar
import threading
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from flask import Flask, render_template, jsonify

from event_market_scanner import (
    PolymarketCollector,
    KalshiCollector,
)

app = Flask(__name__)

# ─── Global scan state ───────────────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    "status": "idle",
    "last_scan": None,
    "stats": {},
    "true_arbs": [],
    "all_spreads": [],
    "error": None,
}

# ─── Config ──────────────────────────────────────────────────────────────────
MATCH_THRESHOLD = 0.50
MIN_SPREAD = 0.01
MAX_RESULTS = 50
SEMANTIC_BONUS = 0.15
TOKEN_WEIGHT = 0.35
FETCH_LIMIT = 1000


# ═════════════════════════════════════════════════════════════════════════════
# Matching & Arbitrage Engine
# ═════════════════════════════════════════════════════════════════════════════

_MONTH_MAP = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTH_MAP.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_ORDINAL_RE = re.compile(r"(\d+)\s*(?:st|nd|rd|th)\b", re.I)
_MONTH_NAMES = "|".join(list(calendar.month_name[1:]) + list(calendar.month_abbr[1:]))
_BEFORE_DATE_RE = re.compile(rf"\bbefore\s+({_MONTH_NAMES})\s+(\d{{1,2}})(?:\s*(?:st|nd|rd|th))?(?:\s*,?\s*(\d{{4}}))?", re.I)
_BY_DATE_RE = re.compile(rf"\bby\s+({_MONTH_NAMES})\s+(\d{{1,2}})(?:\s*(?:st|nd|rd|th))?(?:\s*,?\s*(\d{{4}}))?", re.I)
_BY_END_OF_RE = re.compile(rf"\bby\s+(?:the\s+)?end\s+of\s+({_MONTH_NAMES})\s*,?\s*(\d{{4}})?", re.I)
_QUARTER_ENDS = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_BY_END_Q_RE = re.compile(r"\bby\s+(?:the\s+)?end\s+of\s+q([1-4])\s*,?\s*(\d{4})?", re.I)

_ORDINAL_WORDS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
                  "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10"}

_SYNONYM_GROUPS = [
    (["more than", "greater than", "over", "above", "exceeding", "exceed", "higher than", "in excess of"], "more than"),
    (["less than", "fewer than", "under", "below", "lower than"], "less than"),
    (["at least", "no fewer than", "no less than", "a minimum of", "or more"], "at least"),
    (["at most", "no more than", "a maximum of", "or fewer", "or less"], "at most"),
    (["increase", "rise", "go up", "gain", "climb", "grow", "surge", "jump"], "increase"),
    (["decrease", "fall", "go down", "decline", "drop", "sink", "plunge", "slide"], "decrease"),
    (["win", "defeat", "beat", "prevail"], "win"),
    (["confirm", "approve", "ratify", "pass"], "confirm"),
    (["announce", "reveal", "disclose", "unveil"], "announce"),
]

_NAME_VARIANTS = {
    "donald trump": "trump", "donald j trump": "trump", "president trump": "trump",
    "joe biden": "biden", "joseph biden": "biden", "president biden": "biden",
    "kamala harris": "harris", "vice president harris": "harris",
    "j d vance": "jd vance", "j.d. vance": "jd vance",
    "ron desantis": "desantis", "ronald desantis": "desantis",
    "elon musk": "musk",
    "jerome powell": "powell", "jay powell": "powell", "chair powell": "powell",
    "federal reserve": "fed", "the fed": "fed",
    "european central bank": "ecb",
    "united states": "us", "united states of america": "us",
    "united kingdom": "uk",
    "s&p 500": "sp500", "s&p500": "sp500", "s and p 500": "sp500",
    "bitcoin": "btc", "ethereum": "eth",
}

_NEGATION_PATTERNS = [
    re.compile(r"\bwill\s+not\b", re.I),
    re.compile(r"\bwon't\b", re.I),
    re.compile(r"\bnot\s+(?:be|happen|occur|pass|win)\b", re.I),
    re.compile(r"\bfail(?:s)?\s+to\b", re.I),
    re.compile(r"\bno\s+(?:new|further|additional)\b", re.I),
]


def _last_day(year, month):
    return calendar.monthrange(year, month)[1]


def _canon_date(month_str, day, year):
    m = _MONTH_MAP.get(month_str.lower())
    if m is None:
        return ""
    y = year or 2026
    d = min(day, _last_day(y, m))
    return f"by {y:04d}-{m:02d}-{d:02d}"


def _normalise_dates(text):
    def _before_repl(m):
        mon, day, yr = m.group(1), int(m.group(2)), m.group(3)
        mi = _MONTH_MAP.get(mon.lower())
        if mi is None:
            return m.group(0)
        y = int(yr) if yr else 2026
        d = date(y, mi, min(int(day), _last_day(y, mi)))
        prev = d - timedelta(days=1)
        return f"by {prev.year:04d}-{prev.month:02d}-{prev.day:02d}"
    text = _BEFORE_DATE_RE.sub(_before_repl, text)

    def _by_repl(m):
        return _canon_date(m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
    text = _BY_DATE_RE.sub(_by_repl, text)

    def _by_end_repl(m):
        mon, yr = m.group(1), m.group(2)
        mi = _MONTH_MAP.get(mon.lower())
        if mi is None:
            return m.group(0)
        y = int(yr) if yr else 2026
        return f"by {y:04d}-{mi:02d}-{_last_day(y, mi):02d}"
    text = _BY_END_OF_RE.sub(_by_end_repl, text)

    def _q_repl(m):
        q, yr = int(m.group(1)), m.group(2)
        y = int(yr) if yr else 2026
        em, ed = _QUARTER_ENDS[q]
        return f"by {y:04d}-{em:02d}-{ed:02d}"
    text = _BY_END_Q_RE.sub(_q_repl, text)
    return text


def _normalise_numbers(text):
    for word, digit in _ORDINAL_WORDS.items():
        text = re.sub(rf"\b{word}\b", digit, text, flags=re.I)
    text = _ORDINAL_RE.sub(r"\1", text)
    text = re.sub(r"\$\s*([\d,.]+)\s*[kK]\b", lambda m: "$" + str(int(float(m.group(1).replace(",", "")) * 1000)), text)
    text = re.sub(r"\$\s*([\d,.]+)\s*[mM]\b", lambda m: "$" + str(int(float(m.group(1).replace(",", "")) * 1_000_000)), text)
    text = re.sub(r"\$\s*([\d,.]+)\s*[bB]\b", lambda m: "$" + str(int(float(m.group(1).replace(",", "")) * 1_000_000_000)), text)
    text = re.sub(r"(\d),(\d{3})", r"\1\2", text)
    text = re.sub(r"(\d),(\d{3})", r"\1\2", text)
    text = re.sub(r"\bpercent\b", "%", text, flags=re.I)
    text = re.sub(r"\bpct\b", "%", text, flags=re.I)
    return text


def _normalise_synonyms(text):
    for synonyms, canonical in _SYNONYM_GROUPS:
        for syn in synonyms:
            if syn == canonical:
                continue
            text = re.sub(rf"\b{re.escape(syn)}\b", canonical, text, flags=re.I)
    return text


def _normalise_names(text):
    for variant, canon in sorted(_NAME_VARIANTS.items(), key=lambda x: -len(x[0])):
        text = re.sub(rf"\b{re.escape(variant)}\b", canon, text, flags=re.I)
    return text


def _has_negation(text):
    return any(p.search(text) for p in _NEGATION_PATTERNS)


def _strip_negation(text):
    text = re.sub(r"\bwill\s+not\b", "will", text, flags=re.I)
    text = re.sub(r"\bwon't\b", "will", text, flags=re.I)
    text = re.sub(r"\bnot\s+", " ", text, flags=re.I)
    text = re.sub(r"\bfail(?:s)?\s+to\b", "", text, flags=re.I)
    return text


def _normalise(text):
    text = text.lower().strip()
    text = _normalise_dates(text)
    text = _normalise_numbers(text)
    text = _normalise_synonyms(text)
    text = _normalise_names(text)
    text = re.sub(r"""['''"?!.,;:()\[\]{}&]""", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_key_tokens(text):
    normed = _normalise(text)
    tokens = set()
    tokens.update(re.findall(r"\$\d+", normed))
    tokens.update(re.findall(r"\b\d{2,}\b", normed))
    tokens.update(re.findall(r"\d{4}-\d{2}-\d{2}", normed))
    _STOP = {"will", "what", "when", "where", "which", "that", "this", "with", "from",
             "have", "been", "does", "than", "more", "less", "into", "also", "each",
             "they", "them", "then", "some", "most", "about", "their", "there", "would",
             "could", "should", "being", "other", "after", "before", "between"}
    for w in normed.split():
        if len(w) >= 4 and w not in _STOP and not w.startswith("$"):
            tokens.add(w)
    return tokens


def _combined_score(a, b):
    raw_a, raw_b = a, b
    na, nb = _normalise(a), _normalise(b)
    fuzzy = SequenceMatcher(None, na, nb).ratio()
    ta, tb = _extract_key_tokens(a), _extract_key_tokens(b)
    jaccard = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    score = (1.0 - TOKEN_WEIGHT) * fuzzy + TOKEN_WEIGHT * jaccard
    raw_fuzzy = SequenceMatcher(None, raw_a.lower(), raw_b.lower()).ratio()
    if fuzzy > raw_fuzzy + 0.05:
        score = min(1.0, score + SEMANTIC_BONUS)

    neg_a, neg_b = _has_negation(raw_a), _has_negation(raw_b)
    inverted = neg_a != neg_b
    if inverted:
        stripped_a = _strip_negation(na)
        stripped_b = _strip_negation(nb)
        stripped_fuzzy = SequenceMatcher(None, stripped_a, stripped_b).ratio()
        if stripped_fuzzy > fuzzy:
            score = (1.0 - TOKEN_WEIGHT) * stripped_fuzzy + TOKEN_WEIGHT * jaccard
            if stripped_fuzzy > raw_fuzzy + 0.05:
                score = min(1.0, score + SEMANTIC_BONUS)
    return score, inverted


def _best_match(needle, haystack, threshold):
    best_idx, best_score, best_inv = -1, 0.0, False
    for i, h in enumerate(haystack):
        score, inv = _combined_score(needle, h)
        if score > best_score:
            best_score, best_idx, best_inv = score, i, inv
    if best_score >= threshold:
        return best_idx, best_score, best_inv
    return None


def _extract_outcomes(event):
    outcomes = {}
    for m in event.markets:
        if not m.outcomes:
            continue
        names = {o.name for o in m.outcomes}
        if len(m.outcomes) == 2 and names == {"Yes", "No"}:
            yes_p = next(o.price for o in m.outcomes if o.name == "Yes")
            no_p = next(o.price for o in m.outcomes if o.name == "No")
            label = m.question.strip()
            for prefix in [event.title + " ", "Will ", "Will the "]:
                if label.startswith(prefix):
                    label = label[len(prefix):]
                    break
            label = label.rstrip("?").strip()
            if label:
                label = label[0].upper() + label[1:]
            else:
                label = m.question
            key = _normalise(label)
            outcomes[key] = {"label": label, "yes_price": round(yes_p, 4), "no_price": round(no_p, 4),
                             "overround": round(yes_p + no_p, 4), "market": m.question, "liquidity": m.liquidity}
        else:
            for o in m.outcomes:
                if not o.name:
                    continue
                key = _normalise(o.name)
                outcomes[key] = {"label": o.name, "yes_price": round(o.price, 4),
                                 "no_price": round(1.0 - o.price, 4), "overround": 1.0,
                                 "market": m.question, "liquidity": m.liquidity}
    return outcomes


# ═════════════════════════════════════════════════════════════════════════════
# Full Scan
# ═════════════════════════════════════════════════════════════════════════════

def run_scan():
    global _state
    with _lock:
        _state["status"] = "scanning"
        _state["error"] = None

    try:
        poly_events = PolymarketCollector().collect(limit=FETCH_LIMIT)
        kalshi_events = KalshiCollector().collect(limit=FETCH_LIMIT)

        # Match events
        matched, used_k = [], set()
        for pe in poly_events:
            best_ki, best_score = -1, 0.0
            for ki, ke in enumerate(kalshi_events):
                if ki in used_k:
                    continue
                score, _ = _combined_score(pe.title, ke.title)
                if score > best_score:
                    best_score, best_ki = score, ki
            if best_score >= MATCH_THRESHOLD and best_ki >= 0:
                matched.append((pe, kalshi_events[best_ki], best_score))
                used_k.add(best_ki)

        # Detect arbitrage
        arb_rows = []
        for pe, ke, event_sim in matched:
            poly_outs = _extract_outcomes(pe)
            kalshi_outs = _extract_outcomes(ke)
            if not poly_outs or not kalshi_outs:
                continue

            k_keys = list(kalshi_outs.keys())
            k_labels = [kalshi_outs[k]["label"] for k in k_keys]
            used = set()

            for pk, pd in poly_outs.items():
                if pk in kalshi_outs and pk not in used:
                    kd, mscore, inv = kalshi_outs[pk], 1.0, False
                    used.add(pk)
                else:
                    result = _best_match(pd["label"], k_labels, MATCH_THRESHOLD)
                    if not result:
                        continue
                    idx, mscore, inv = result
                    kk = k_keys[idx]
                    if kk in used:
                        continue
                    kd = kalshi_outs[kk]
                    used.add(kk)

                py, pn = pd["yes_price"], pd["no_price"]
                ky = kd["no_price"] if inv else kd["yes_price"]
                kn = kd["yes_price"] if inv else kd["no_price"]

                cost_a, cost_b = py + kn, ky + pn
                profit_a, profit_b = max(0, 1.0 - cost_a), max(0, 1.0 - cost_b)

                if profit_a >= profit_b:
                    best_cost, best_profit, strat = cost_a, profit_a, "A"
                    direction = "Buy Yes @ Poly + No @ Kalshi"
                else:
                    best_cost, best_profit, strat = cost_b, profit_b, "B"
                    direction = "Buy Yes @ Kalshi + No @ Poly"

                if best_profit == 0 and cost_a == cost_b:
                    direction = "No edge"
                if inv:
                    direction += " (inverted)"

                yes_spread = abs(py - ky)
                no_spread = abs(pn - kn)
                if max(yes_spread, no_spread) >= MIN_SPREAD:
                    roi = (best_profit / best_cost * 100) if best_cost > 0 else 0
                    arb_rows.append({
                        "event": pe.title, "event_sim": round(event_sim, 2),
                        "outcome": pd["label"], "outcome_kalshi": kd["label"],
                        "outcome_sim": round(mscore, 2), "inverted": inv,
                        "poly_yes": py, "poly_no": pn, "kalshi_yes": ky, "kalshi_no": kn,
                        "poly_overround": pd["overround"], "kalshi_overround": kd["overround"],
                        "yes_spread": round(yes_spread, 4), "no_spread": round(no_spread, 4),
                        "cost_a": round(cost_a, 4), "cost_b": round(cost_b, 4),
                        "profit_a": round(profit_a, 4), "profit_b": round(profit_b, 4),
                        "best_strategy": strat, "best_cost": round(best_cost, 4),
                        "arb_profit": round(best_profit, 4), "roi": round(roi, 1),
                        "direction": direction,
                        "poly_liq": pd["liquidity"], "kalshi_liq": kd["liquidity"],
                        "poly_url": pe.url, "kalshi_url": ke.url,
                    })

        arb_rows.sort(key=lambda r: (r["arb_profit"], max(r["yes_spread"], r["no_spread"])), reverse=True)
        true_arbs = [r for r in arb_rows if r["arb_profit"] > 0]

        stats = {
            "poly_count": len(poly_events), "kalshi_count": len(kalshi_events),
            "matched_count": len(matched), "total_spreads": len(arb_rows),
            "true_arb_count": len(true_arbs),
            "inverted_count": sum(1 for r in arb_rows if r["inverted"]),
        }

        with _lock:
            _state.update(status="done", last_scan=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          stats=stats, true_arbs=true_arbs[:MAX_RESULTS],
                          all_spreads=arb_rows[:MAX_RESULTS])

    except Exception as e:
        with _lock:
            _state["status"] = "error"
            _state["error"] = str(e)


# ═════════════════════════════════════════════════════════════════════════════
# Routes
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(status=_state["status"], last_scan=_state["last_scan"], error=_state["error"])


@app.route("/api/scan", methods=["POST"])
def api_scan():
    with _lock:
        if _state["status"] == "scanning":
            return jsonify(status="already_scanning")
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify(status="started")


@app.route("/api/results")
def api_results():
    with _lock:
        return jsonify(status=_state["status"], last_scan=_state["last_scan"],
                       stats=_state["stats"], true_arbs=_state["true_arbs"],
                       all_spreads=_state["all_spreads"], error=_state["error"])


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    print(f"\n  Event Market Scanner — http://localhost:{args.port}\n")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug)
