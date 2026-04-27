#!/usr/bin/env python3
"""
Cortana — Edge Room morning briefing.

Pulls live moneyline odds from The Odds API, finds the highest-½-Kelly opportunity
across the configured sports, and pushes a short briefing to a Telegram chat.
Mirrors the math of the Edge Room dashboard: per-book de-vig, leave-one-out
median fair prob across other books, ½ Kelly with a 15% prob floor and a hard cap.

────────────────────────────────────────────────────────────────────────────
HOW TO RUN

    export ODDS_API_KEY="your-the-odds-api-key"
    export EDGE_BANKROLL="1000"
    export EDGE_SPORTS="icehockey_nhl,basketball_nba"      # comma-separated
    export EDGE_REGION="us"                                # us | us2 | uk | eu | au
    export EDGE_KELLY_CAP="3"                              # percent of bankroll
    export TELEGRAM_BOT_TOKEN="123456:ABC..."
    export TELEGRAM_CHAT_ID="123456789"
    python3 cortana_edgeroom_briefing.py

CRONTAB — every morning at 8:00 local time:

    0 8 * * *  /usr/bin/python3 /home/ty/cortana_edgeroom_briefing.py >> /var/log/cortana-edgeroom.log 2>&1

(set the env vars in /etc/environment, ~/.profile, or wrap in a shell script
that exports them and then calls python3.)

Pure stdlib only — no pip install required.
────────────────────────────────────────────────────────────────────────────
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from statistics import median


# ---------- env ----------------------------------------------------------------

REQUIRED = [
    "ODDS_API_KEY",
    "EDGE_BANKROLL",
    "EDGE_SPORTS",
    "EDGE_REGION",
    "EDGE_KELLY_CAP",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


def load_env():
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        sys.stderr.write("Missing env vars: " + ", ".join(missing) + "\n")
        sys.exit(2)
    return {
        "api_key": os.environ["ODDS_API_KEY"].strip(),
        "bankroll": float(os.environ["EDGE_BANKROLL"]),
        "sports": [s.strip() for s in os.environ["EDGE_SPORTS"].split(",") if s.strip()],
        "region": os.environ["EDGE_REGION"].strip(),
        "cap_pct": float(os.environ["EDGE_KELLY_CAP"]),
        "tg_token": os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        "tg_chat": os.environ["TELEGRAM_CHAT_ID"].strip(),
    }


# ---------- odds math (mirrors edgeroom.html) ----------------------------------

PROB_FLOOR = 0.15  # filter out longshots; matches dashboard


def american_to_implied(o):
    return -o / (-o + 100) if o < 0 else 100 / (o + 100)


def american_to_decimal(o):
    return 1 + 100 / -o if o < 0 else 1 + o / 100


def half_kelly_pct(dec_odds, fair_prob):
    b = dec_odds - 1
    if b <= 0:
        return 0.0
    q = 1 - fair_prob
    k = (b * fair_prob - q) / b
    return max(0.0, k * 0.5 * 100)


def fetch_sport(api_key, sport, region):
    """Hit The Odds API for one sport, h2h market only."""
    qs = urllib.parse.urlencode({
        "apiKey": api_key,
        "regions": region,
        "markets": "h2h",
        "oddsFormat": "american",
    })
    url = "https://api.the-odds-api.com/v4/sports/{}/odds/?{}".format(sport, qs)
    req = urllib.request.Request(url, headers={"User-Agent": "cortana-edge-briefing/1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_opportunities(events):
    """Same algorithm as the dashboard's buildOpportunities() for moneyline."""
    opps = []
    for ev in events or []:
        # bookFair[book][outcome_name] = de-vigged prob from THAT book alone
        book_fair = {}
        # by_outcome[outcome_name] = list of {book, american, decimal}
        by_outcome = {}
        for bm in ev.get("bookmakers") or []:
            market = next((m for m in (bm.get("markets") or []) if m.get("key") == "h2h"), None)
            if not market:
                continue
            outs = market.get("outcomes") or []
            if len(outs) < 2:
                continue
            imps = {}
            total = 0.0
            for o in outs:
                name = o.get("name")
                price = o.get("price")
                if name is None or price is None:
                    continue
                imp = american_to_implied(price)
                imps[name] = imp
                total += imp
                by_outcome.setdefault(name, []).append({
                    "book": bm.get("title", "?"),
                    "american": price,
                    "decimal": american_to_decimal(price),
                })
            if total <= 0 or len(imps) < 2:
                continue
            book_fair[bm.get("title", "?")] = {n: imps[n] / total for n in imps}

        all_books = list(book_fair.keys())
        if len(all_books) < 2:
            continue

        for outcome_name, offers in by_outcome.items():
            for offer in offers:
                others = [book_fair[b][outcome_name] for b in all_books
                          if b != offer["book"] and outcome_name in book_fair[b]]
                if not others:
                    continue
                fp = median(others)
                if fp <= 0 or fp >= 1:
                    continue
                opps.append({
                    "matchup": "{} @ {}".format(ev.get("away_team", "?"), ev.get("home_team", "?")),
                    "pick": outcome_name + " ML",
                    "book": offer["book"],
                    "american": offer["american"],
                    "decimal": offer["decimal"],
                    "fair_prob": fp,
                    "ev_pct": (fp * offer["decimal"] - 1) * 100,
                })
    return opps


# ---------- briefing -----------------------------------------------------------

def fmt_am(o):
    return ("+" if o > 0 else "") + str(int(o))


def pick_top(all_opps, cap_pct):
    """Filter to prob_floor (or full pool if nothing clears), rank by ½ Kelly."""
    pool = [o for o in all_opps if o["fair_prob"] >= PROB_FLOOR] or all_opps
    for o in pool:
        o["half_kelly"] = half_kelly_pct(o["decimal"], o["fair_prob"])
    pool.sort(key=lambda o: o["half_kelly"], reverse=True)
    if not pool or pool[0]["half_kelly"] <= 0 or pool[0]["ev_pct"] < 0.5:
        return None
    top = pool[0]
    raw = top["half_kelly"]
    top["half_kelly_capped"] = min(raw, cap_pct)
    top["was_capped"] = raw > cap_pct
    return top


def format_briefing(top, bankroll, snapshot_dt):
    half_k = top["half_kelly_capped"]
    dollar = round(bankroll * half_k / 100) if bankroll > 0 else 0
    cap_tag = " (capped)" if top["was_capped"] else ""
    return "\n".join([
        "────────────",
        "Edge Room briefing — " + snapshot_dt.strftime("%a %b %d"),
        "Top pick: " + top["pick"],
        "Match: " + top["matchup"],
        "Best price: {} @ {}".format(top["book"], fmt_am(top["american"])),
        "Fair prob: {:.1f}% · Edge: +{:.1f}% · ½ Kelly: {:.1f}%{}".format(
            top["fair_prob"] * 100, top["ev_pct"], half_k, cap_tag),
        "Bankroll @ ${:.0f} → suggested ${}".format(bankroll, dollar),
        "Snapshot: " + snapshot_dt.strftime("%H:%M"),
        "────────────",
    ])


def send_telegram(token, chat_id, text):
    url = "https://api.telegram.org/bot{}/sendMessage".format(token)
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


# ---------- main ---------------------------------------------------------------

def main():
    cfg = load_env()
    snapshot = datetime.now()

    all_opps = []
    for sport in cfg["sports"]:
        try:
            events = fetch_sport(cfg["api_key"], sport, cfg["region"])
            all_opps.extend(build_opportunities(events))
        except Exception as e:
            sys.stderr.write("Fetch failed for {}: {}\n".format(sport, e))

    top = pick_top(all_opps, cfg["cap_pct"])
    if not top:
        msg = "Edge Room briefing — " + snapshot.strftime("%a %b %d") + "\nNo clear edge in today's odds."
    else:
        msg = format_briefing(top, cfg["bankroll"], snapshot)

    status, body = send_telegram(cfg["tg_token"], cfg["tg_chat"], msg)
    if status != 200:
        sys.stderr.write("Telegram POST returned {}: {}\n".format(status, body))
        sys.exit(1)


if __name__ == "__main__":
    main()
