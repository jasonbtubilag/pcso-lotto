#!/usr/bin/env python3
"""
PCSO Lotto Results Auto-Updater
================================
Re-scrapes the PCSO "Search Lotto Result by Date" page, maintains a local
database (CSV + JSON), and regenerates a self-contained HTML dashboard that
shows today's scheduled draws and the full results history.

Tracks 7 games: Ultra Lotto 6/58, Grand Lotto 6/55, Super Lotto 6/49,
Mega Lotto 6/45, Lotto 6/42, plus the positional-digit games 4D Lotto and
6D Lotto (each digit 0-9, exact order).

Designed to be run once per day (e.g. 10:00 PM) via cron / Task Scheduler.

Requires:  pip install requests beautifulsoup4
Usage:     python pcso_update.py
"""

import csv
import json
import os
import sys
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
URL = "https://www.pcso.gov.ph/searchlottoresult.aspx"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE  = os.path.join(OUT_DIR, "pcso_lotto_results.csv")
JSON_FILE = os.path.join(OUT_DIR, "pcso_lotto_results.json")
HTML_FILE = os.path.join(OUT_DIR, "pcso_lotto_results.html")
JACKPOT_FILE = os.path.join(OUT_DIR, "current_jackpots.json")

# Raw label on the PCSO site  ->  normalized display name. Several spelling
# variants are mapped because the PCSO results table is inconsistent across games.
NAME_MAP = {
    "Ultra Lotto 6/58": "Ultra Lotto 6/58",
    "Grand Lotto 6/55": "Grand Lotto 6/55",
    "Superlotto 6/49":  "Super Lotto 6/49",
    "Megalotto 6/45":   "Mega Lotto 6/45",
    "Lotto 6/42":       "Lotto 6/42",
    # digit games (positional, digits 0-9, exact order)
    "4D Lotto":         "4D Lotto",
    "6D Lotto":         "6D Lotto",
    "4Digit Game":      "4D Lotto",
    "6Digit Game":      "6D Lotto",
    "4-Digit":          "4D Lotto",
    "6-Digit":          "6D Lotto",
}

# Weekly draw schedule (0 = Sunday ... 6 = Saturday)
SCHEDULE = {
    "Ultra Lotto 6/58": [0, 2, 5],   # Sun, Tue, Fri
    "Grand Lotto 6/55": [1, 3, 6],   # Mon, Wed, Sat
    "Super Lotto 6/49": [0, 2, 4],   # Sun, Tue, Thu
    "Mega Lotto 6/45":  [1, 3, 5],   # Mon, Wed, Fri
    "Lotto 6/42":       [2, 4, 6],   # Tue, Thu, Sat
    "4D Lotto":         [1, 3, 5],   # Mon, Wed, Fri
    "6D Lotto":         [2, 4, 6],   # Tue, Thu, Sat
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

FIELD = "ctl00$ctl00$cphContainer$cpContent$"


def iso(date_str):
    """Convert M/D/YYYY -> YYYY-MM-DD."""
    m, d, y = (int(x) for x in date_str.split("/"))
    return f"{y:04d}-{m:02d}-{d:02d}"


def hidden_fields(soup):
    return {i.get("name"): i.get("value", "")
            for i in soup.select("input[type=hidden]") if i.get("name")}


def fetch_range(session, start, end):
    """POST the search form for a date range and return list of result dicts."""
    r0 = session.get(URL, timeout=30)
    soup0 = BeautifulSoup(r0.text, "html.parser")
    data = hidden_fields(soup0)
    data[FIELD + "ddlStartMonth"] = MONTHS[start.month - 1]
    data[FIELD + "ddlStartDate"]  = str(start.day)
    data[FIELD + "ddlStartYear"]  = str(start.year)
    data[FIELD + "ddlEndMonth"]   = MONTHS[end.month - 1]
    data[FIELD + "ddlEndDay"]     = str(end.day)
    data[FIELD + "ddlEndYear"]    = str(end.year)
    data[FIELD + "ddlSelectGame"] = "0"
    data[FIELD + "btnSearch"]     = "Search Lotto"

    r1 = session.post(URL, data=data, timeout=60)
    soup1 = BeautifulSoup(r1.text, "html.parser")

    out = []
    for tr in soup1.select("table tr"):
        cells = [td.get_text(strip=True) for td in tr.select("td")]
        if len(cells) >= 5 and cells[0] in NAME_MAP:
            date = cells[2]
            if "/" not in date:
                continue
            out.append({
                "game": NAME_MAP[cells[0]],
                "date": iso(date),
                "combination": cells[1],
                "jackpot": cells[3],
                "winners": cells[4],
            })
    return out


# ---------------------------------------------------------------------------
# Fallback mirror (philnews.ph) — used only when the official PCSO page has
# not yet published a scheduled same-day draw. PCSO's own search-by-date page
# typically lags by several hours; philnews posts each game minutes after the
# 9 PM draw, with the winning numbers present in static HTML.
# ---------------------------------------------------------------------------
MIRROR_SLUG = {
    "Ultra Lotto 6/58": "6-58",
    "Grand Lotto 6/55": "6-55",
    "Super Lotto 6/49": "6-49",
    "Mega Lotto 6/45":  "6-45",
    "Lotto 6/42":       "6-42",
    "4D Lotto":         "4d",
    "6D Lotto":         "6d",
}
GAME_MAX = {  # highest valid ball number per 6/N game (sanity check)
    "Ultra Lotto 6/58": 58, "Grand Lotto 6/55": 55, "Super Lotto 6/49": 49,
    "Mega Lotto 6/45": 45, "Lotto 6/42": 42,
}
# Positional-digit games: P digits, each 0-9, order matters, repeats allowed.
DIGIT_POS = {"4D Lotto": 4, "6D Lotto": 6}


def _mirror_url(game, d):
    """Build the philnews dated per-game post URL for a draw date `d`."""
    slug = MIRROR_SLUG[game]
    wd = d.strftime("%A").lower()      # e.g. "friday"
    mo = d.strftime("%B").lower()      # e.g. "june"
    return (f"https://philnews.ph/{d.year}/{d.month:02d}/{d.day:02d}/"
            f"{slug}-lotto-result-today-{wd}-{mo}-{d.day}-{d.year}/")


def parse_mirror_html(html, game, d):
    """Pure parser for a philnews dated per-game post. Returns a record dict
    matching the PCSO schema, or None if the page does not yet contain a valid,
    date-matching result. Never raises. (Separated from network I/O so it can
    be unit-tested offline.)

    IMPORTANT: a philnews page for a draw date is published *before* the 9 PM
    draw, with the winning-number cell blank and a "Previous result" table lower
    down. We must NOT grab those previous numbers. So we only accept a 6-number
    group that sits *between the target date and the following "Jackpot" label*
    — i.e. inside the primary result table for that exact date. While the draw
    is pending, that cell is blank, so we correctly return None.
    """
    import re
    try:
        text = BeautifulSoup(html, "html.parser").get_text("\n")
        long_date = f"{MONTHS[d.month - 1]} {d.day}, {d.year}"
        is_digit = game in DIGIT_POS
        if is_digit:
            P = DIGIT_POS[game]
            # P single digits, dash-separated (e.g. "0-4-1-3" / "5-8-2-8-1-6").
            num_re = re.compile(r"\b(\d(?:-\d){%d})\b" % (P - 1))
        else:
            hi = GAME_MAX[game]
            num_re = re.compile(r"\b(\d{1,2}(?:-\d{1,2}){5})\b")

        for mt in re.finditer(re.escape(long_date), text):
            seg = text[mt.end(): mt.end() + 250]
            jpos = seg.find("Jackpot")
            if jpos == -1:           # not the primary result table for this date
                continue
            nm = num_re.search(seg[:jpos])   # numbers must appear before "Jackpot"
            if not nm:               # blank cell -> result not posted yet
                continue
            parts = [int(x) for x in nm.group(1).split("-")]
            if is_digit:
                # digits repeat freely; only validate count and 0-9 range.
                if len(parts) != P or any(n < 0 or n > 9 for n in parts):
                    continue
                combo = "-".join(str(n) for n in parts)
            else:
                if len(set(parts)) != 6 or any(n < 1 or n > hi for n in parts):
                    continue
                combo = "-".join(f"{n:02d}" for n in parts)

            jm = re.search(r"₱\s*([\d,]+\.\d{2})", seg[jpos:])
            jackpot = jm.group(1) if jm else ""
            wm = re.search(r"Jackpot Winner\(s\)[\s\S]{0,40}?(\d+)", seg[jpos:])
            winners = wm.group(1) if wm else "0"

            return {
                "game": game,
                "date": f"{d.year:04d}-{d.month:02d}-{d.day:02d}",
                "combination": combo,
                "jackpot": jackpot,
                "winners": winners,
            }
        return None
    except Exception:
        return None


def mirror_fetch(session, game, d):
    """Fetch one (game, date) result from the philnews mirror and parse it.
    Returns a record dict or None. Never raises."""
    try:
        r = session.get(_mirror_url(game, d), timeout=30)
        if r.status_code != 200:
            return None
        return parse_mirror_html(r.text, game, d)
    except Exception:
        return None


# philnews "6/NN" code -> our normalized game name
GAME_BY_CODE = {
    "6/58": "Ultra Lotto 6/58", "6/55": "Grand Lotto 6/55",
    "6/49": "Super Lotto 6/49", "6/45": "Mega Lotto 6/45", "6/42": "Lotto 6/42",
}


def parse_current_jackpots(html):
    """Pure parser: extract the *upcoming* jackpot per game from philnews'
    'complete list of the jackpot prizes' block. Returns {game: 'amount'}.
    Never raises. (Separated from network I/O for offline unit-testing.)"""
    import re
    try:
        text = BeautifulSoup(html, "html.parser").get_text("\n")
        idx = text.find("complete list of the jackpot")
        region = text[idx:] if idx != -1 else text
        out = {}
        for code, game in GAME_BY_CODE.items():
            m = re.search(re.escape(code) + r"[\s\S]{0,40}?₱\s*([\d,]+\.\d{2})", region)
            if m:
                out[game] = m.group(1)
        return out
    except Exception:
        return {}


def fetch_current_jackpots(session):
    """Best-effort: pull today's upcoming jackpots from philnews and write them
    to current_jackpots.json (so the dashboard can show what's up for grabs).
    Never raises; leaves any prior file intact on failure."""
    try:
        r = session.get("https://philnews.ph/lotto-result-today/", timeout=30)
        if r.status_code != 200:
            return None
        jp = parse_current_jackpots(r.text)
        if jp:
            with open(JACKPOT_FILE, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": datetime.now().isoformat(timespec="seconds"),
                           "jackpots": jp}, f, indent=2, ensure_ascii=False)
            print(f"  current jackpots updated for {len(jp)} game(s).", flush=True)
        return jp
    except Exception:
        return None


def recent_missing_draws(existing, days_back=4):
    """Return [(game, date_obj), ...] for scheduled draws in the last `days_back`
    days that are NOT yet in the database (so we can backfill from the mirror)."""
    have = {(r["game"], r["date"]) for r in existing}
    today = datetime.now().date()
    missing = []
    for back in range(days_back + 1):
        d = today - timedelta(days=back)
        our_dow = (d.weekday() + 1) % 7          # python Mon=0 -> our Sun=0
        for game, days in SCHEDULE.items():
            if game not in MIRROR_SLUG:           # mirror only covers the 6/N games
                continue
            if our_dow in days:
                iso_d = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
                if (game, iso_d) not in have:
                    missing.append((game, datetime(d.year, d.month, d.day)))
    return missing


def load_existing():
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def key(r):
    return (r["game"], r["date"], r["combination"])


def full_history(session, start_year=2016):
    """Scrape the entire history, year by year (used for first run)."""
    rows = []
    this_year = datetime.now().year
    for y in range(start_year, this_year + 1):
        print(f"  fetching {y} ...", flush=True)
        rows += fetch_range(session,
                            datetime(y, 1, 1),
                            datetime(y, 12, 31))
    return rows


def save_csv(records):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Game", "Draw Date", "Winning Combination",
                    "Jackpot (PHP)", "Winners"])
        for r in records:
            w.writerow([r["game"], r["date"], r["combination"],
                        r["jackpot"], r["winners"]])


def save_json(records):
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def build_html(records):
    generated = datetime.now().isoformat(timespec="seconds")
    data_json = json.dumps(records, ensure_ascii=False)
    sched_json = json.dumps(SCHEDULE)
    html = HTML_TEMPLATE.replace("__DATA__", data_json) \
                        .replace("__SCHEDULE__", sched_json) \
                        .replace("__GENERATED__", json.dumps(generated))
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (PCSO-Updater)"})

    existing = load_existing()
    seen = {key(r) for r in existing}

    if existing:
        # Incremental: only pull the last ~30 days for new draws.
        print("Updating recent results ...", flush=True)
        end = datetime.now()
        start = end - timedelta(days=30)
        try:
            fresh = fetch_range(session, start, end)
        except Exception as exc:  # noqa: BLE001
            # Official PCSO page unreachable — don't abort; let the philnews
            # mirror fallback below try to fill in today's scheduled draws.
            print(f"  (official PCSO fetch failed: {exc}; trying mirror)",
                  flush=True)
            fresh = []
    else:
        # First run: scrape the entire history.
        print("No existing database found. Building full history (2016-now)...",
              flush=True)
        fresh = full_history(session)

    added = 0
    for r in fresh:
        if key(r) not in seen:
            seen.add(key(r))
            existing.append(r)
            added += 1

    # Backfill: any tracked game with NO records yet (e.g. 4D/6D just added) gets a
    # full-history scrape so it isn't limited to the last 30 days. Only the missing
    # games' rows are kept; everything else is already covered incrementally.
    if existing:
        present = {r["game"] for r in existing}
        missing_games = [g for g in SCHEDULE if g not in present]
        if missing_games:
            print(f"Backfilling full history for new game(s): {missing_games}",
                  flush=True)
            try:
                for r in full_history(session):
                    if r["game"] in missing_games and key(r) not in seen:
                        seen.add(key(r))
                        existing.append(r)
                        added += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  (backfill scrape failed: {exc})", flush=True)

    # Fallback: if any recent scheduled draw is still missing from the official
    # source, try the philnews mirror so same-day results appear right after the
    # 9 PM draw instead of waiting for PCSO to update its search page.
    have_gd = {(r["game"], r["date"]) for r in existing}
    # Stale-scrape guard: a mis-parse grabs a game's *previous* posted result —
    # i.e. that game's most recent draw already on file. Compare only against
    # that latest combo (not all history) so legitimate repeats — common for the
    # 4D game's 10,000-combo space — are still accepted.
    latest_combo = {}
    for r in sorted(existing, key=lambda x: x["date"]):
        latest_combo[r["game"]] = r["combination"]
    mirror_added = 0
    if existing:  # only run the mirror in incremental mode, not on first build
        for game, d in recent_missing_draws(existing):
            rec = mirror_fetch(session, game, d)
            # If the combo equals this game's most recent stored draw, we almost
            # certainly grabbed the stale "previous result" rather than the
            # (not-yet-posted) current one, so skip it.
            if rec and rec["combination"] == latest_combo.get(rec["game"]):
                print(f"  ! mirror skipped (stale/duplicate combo): "
                      f"{rec['game']} {rec['date']} {rec['combination']}",
                      flush=True)
                rec = None
            if rec and (rec["game"], rec["date"]) not in have_gd:
                if key(rec) not in seen:
                    seen.add(key(rec))
                    have_gd.add((rec["game"], rec["date"]))
                    latest_combo[rec["game"]] = rec["combination"]
                    existing.append(rec)
                    added += 1
                    mirror_added += 1
                    print(f"  + mirror: {rec['game']} {rec['date']} "
                          f"{rec['combination']}", flush=True)

    # Refresh the upcoming jackpot per game (what's "to be won" today).
    fetch_current_jackpots(session)

    # Sort newest first, then by game name.
    existing.sort(key=lambda r: (r["date"], r["game"]), reverse=True)

    save_csv(existing)
    save_json(existing)
    build_html(existing)

    src_note = f" ({mirror_added} via mirror)" if mirror_added else ""
    print(f"Done. {added} new draw(s) added{src_note}. "
          f"Total records: {len(existing)}.", flush=True)


# ---------------------------------------------------------------------------
# HTML template (placeholders: __DATA__, __SCHEDULE__, __GENERATED__)
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PCSO Lotto Results Database (2016–2026)</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--accent:#3b82f6;--text:#e2e8f0;--muted:#94a3b8;--border:#334155;}
  *{box-sizing:border-box;}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);}
  header{background:linear-gradient(135deg,#1e3a8a,#3b82f6);padding:24px 20px;}
  header h1{margin:0;font-size:1.6rem;}
  header p{margin:6px 0 0;color:#dbeafe;font-size:.9rem;}
  .wrap{max-width:1200px;margin:0 auto;padding:20px;}
  .today{background:linear-gradient(135deg,#065f46,#10b981);border-radius:12px;padding:18px 20px;margin-bottom:20px;}
  .today h2{margin:0 0 4px;font-size:1.15rem;}
  .today .date{color:#d1fae5;font-size:.85rem;margin-bottom:12px;}
  .today-games{display:flex;flex-wrap:wrap;gap:10px;}
  .today-chip{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.35);border-radius:30px;padding:8px 16px;font-weight:600;font-size:.95rem;}
  .today-none{color:#d1fae5;font-style:italic;}
  .sched{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:20px;overflow:auto;}
  .sched h3{margin:0 0 12px;font-size:1rem;color:var(--accent);}
  .sched table{width:100%;border-collapse:collapse;font-size:.85rem;}
  .sched th,.sched td{padding:8px 6px;text-align:center;border-bottom:1px solid var(--border);}
  .sched th:first-child,.sched td:first-child{text-align:left;white-space:nowrap;}
  .dot{display:inline-block;width:11px;height:11px;border-radius:50%;}
  .sched th.todaycol{background:rgba(16,185,129,.25);border-radius:6px;color:#34d399;}
  .stats{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px;}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 18px;flex:1;min-width:140px;}
  .stat .num{font-size:1.5rem;font-weight:700;color:var(--accent);}
  .stat .lbl{font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;}
  .controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:16px;}
  select,input{background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:9px 12px;font-size:.9rem;}
  input{flex:1;min-width:180px;}
  button{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:9px 16px;font-size:.9rem;cursor:pointer;}
  button:hover{background:#2563eb;}
  .table-wrap{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:auto;max-height:65vh;}
  table.main{width:100%;border-collapse:collapse;font-size:.88rem;}
  table.main th{position:sticky;top:0;background:#0b1220;text-align:left;padding:11px 14px;cursor:pointer;user-select:none;white-space:nowrap;border-bottom:2px solid var(--accent);}
  table.main th:hover{color:var(--accent);}
  table.main td{padding:9px 14px;border-bottom:1px solid var(--border);white-space:nowrap;}
  table.main tr:hover td{background:rgba(59,130,246,.08);}
  .combo{font-family:'SF Mono',Menlo,Consolas,monospace;letter-spacing:1px;color:#fbbf24;}
  .badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.72rem;font-weight:600;}
  .footer{color:var(--muted);font-size:.8rem;margin-top:16px;line-height:1.6;}
  .status{font-size:.82rem;color:var(--muted);margin-left:auto;}
  .win{color:#34d399;font-weight:600;}
  @media (max-width:640px){
    header{padding:18px 16px;}
    header h1{font-size:1.3rem;}
    header p{font-size:.78rem;}
    .wrap{padding:14px;}
    .today,.sched,.stat{padding:14px;}
    .today h2{font-size:1.05rem;}
    .stats{gap:8px;}
    .stat{flex:1 1 calc(50% - 6px);min-width:0;}
    .stat .num{font-size:1.25rem;}
    .controls{gap:8px;}
    select,input,button{width:100%;flex:1 1 100%;min-width:0;}
    .status{margin-left:0;width:100%;}
    table.main th,table.main td{padding:8px 10px;font-size:.8rem;}
    .footer{font-size:.74rem;}
  }
</style>
</head>
<body>
<header>
  <h1>🎰 PCSO Lotto Results Database</h1>
  <p>Ultra Lotto 6/58 · Grand Lotto 6/55 · Super Lotto 6/49 · Mega Lotto 6/45 · Lotto 6/42 · 4D · 6D — 2016 to 2026</p>
</header>
<div class="wrap">
  <div class="today" id="today"></div>
  <div class="sched" id="schedBox"></div>
  <div class="stats" id="stats"></div>
  <div class="controls">
    <select id="gameFilter"><option value="">All Games</option></select>
    <select id="yearFilter"><option value="">All Years</option></select>
    <input id="search" placeholder="Search combination or date (e.g. 12-34 or 2020)…">
    <button id="refreshBtn" title="Attempt to fetch the newest draws directly from PCSO">↻ Refresh latest</button>
    <span class="status" id="status"></span>
  </div>
  <div class="table-wrap">
    <table class="main">
      <thead><tr>
        <th data-k="game">Game ▲▼</th>
        <th data-k="date">Draw Date ▲▼</th>
        <th data-k="combination">Winning Combination</th>
        <th data-k="jackpotNum">Jackpot (PHP) ▲▼</th>
        <th data-k="winnersNum">Winners ▲▼</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <div class="footer">
    <p>Data source: Philippine Charity Sweepstakes Office (pcso.gov.ph). Snapshot generated: <span id="gen"></span>.</p>
    <p id="count"></p>
    <p><strong>About “Refresh latest”:</strong> A static HTML file cannot reliably auto-update on open (PCSO has no public API and browsers block cross-site requests). Run the included Python auto-update script daily at 10pm to keep this database current.</p>
  </div>
</div>
<script>
const RAW = __DATA__;
const SCHEDULE = __SCHEDULE__;
const DAYNAMES = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
const DAYSHORT = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const colors = {'Ultra Lotto 6/58':'#a855f7','Grand Lotto 6/55':'#ef4444','Super Lotto 6/49':'#3b82f6','Mega Lotto 6/45':'#10b981','Lotto 6/42':'#f59e0b','4D Lotto':'#14b8a6','6D Lotto':'#ec4899'};
function num(s){return parseFloat(String(s).replace(/[^0-9.]/g,''))||0;}
let DATA = RAW.map(r=>({...r, jackpotNum:num(r.jackpot), winnersNum:parseInt(r.winners)||0}));
document.getElementById('gen').textContent = __GENERATED__;

// ===== Today's Draws =====
(function renderToday(){
  const now=new Date();
  const dow=now.getDay();
  const todaysGames=Object.keys(SCHEDULE).filter(g=>SCHEDULE[g].includes(dow));
  const dateStr=now.toLocaleDateString('en-US',{weekday:'long',year:'numeric',month:'long',day:'numeric'});
  let chips;
  if(todaysGames.length){
    chips=todaysGames.map(g=>'<span class="today-chip" style="border-color:'+colors[g]+';">🎱 '+g+'</span>').join('');
  }else{
    chips='<span class="today-none">No major lotto draws scheduled for today.</span>';
  }
  document.getElementById('today').innerHTML='<h2>📅 Games Drawn Today</h2><div class="date">'+dateStr+' — draws at 9:00 PM</div><div class="today-games">'+chips+'</div>';
})();

// ===== Weekly Schedule grid =====
(function renderSchedule(){
  const todayDow=new Date().getDay();
  let h='<h3>Weekly Draw Schedule</h3><table><thead><tr><th>Game</th>';
  for(let d=0;d<7;d++){h+='<th class="'+(d===todayDow?'todaycol':'')+'">'+DAYSHORT[d]+'</th>';}
  h+='</tr></thead><tbody>';
  for(const g in SCHEDULE){
    h+='<tr><td><span class="badge" style="background:'+colors[g]+'22;color:'+colors[g]+'">'+g+'</span></td>';
    for(let d=0;d<7;d++){
      h+='<td>'+(SCHEDULE[g].includes(d)?'<span class="dot" style="background:'+colors[g]+'"></span>':'')+'</td>';
    }
    h+='</tr>';
  }
  h+='</tbody></table>';
  document.getElementById('schedBox').innerHTML=h;
})();

// ===== Table =====
const games=[...new Set(RAW.map(r=>r.game))];
const years=[...new Set(RAW.map(r=>r.date.slice(0,4)))].sort().reverse();
const gf=document.getElementById('gameFilter'), yf=document.getElementById('yearFilter');
games.forEach(g=>{const o=document.createElement('option');o.value=g;o.textContent=g;gf.appendChild(o);});
years.forEach(y=>{const o=document.createElement('option');o.value=y;o.textContent=y;yf.appendChild(o);});
let sortKey='date', sortDir=-1;
function render(){
  const g=gf.value, y=yf.value, q=document.getElementById('search').value.toLowerCase().trim();
  let rows=DATA.filter(r=>(!g||r.game===g)&&(!y||r.date.startsWith(y))&&(!q||r.combination.toLowerCase().includes(q)||r.date.includes(q)));
  rows.sort((a,b)=>{let x=a[sortKey],z=b[sortKey];if(typeof x==='number')return (x-z)*sortDir;return (x<z?-1:x>z?1:0)*sortDir;});
  const tb=document.getElementById('tbody');
  tb.innerHTML=rows.slice(0,3000).map(r=>'<tr><td><span class="badge" style="background:'+colors[r.game]+'22;color:'+colors[r.game]+'">'+r.game+'</span></td><td>'+r.date+'</td><td class="combo">'+r.combination+'</td><td>'+r.jackpot+'</td><td class="'+(r.winnersNum>0?'win':'')+'">'+r.winners+'</td></tr>').join('');
  document.getElementById('count').textContent='Showing '+Math.min(rows.length,3000).toLocaleString()+' of '+rows.length.toLocaleString()+' matching draws ('+DATA.length.toLocaleString()+' total in database).';
  const totalJackpot=rows.reduce((s,r)=>s+r.jackpotNum,0);
  const wins=rows.filter(r=>r.winnersNum>0).length;
  document.getElementById('stats').innerHTML='<div class="stat"><div class="num">'+rows.length.toLocaleString()+'</div><div class="lbl">Draws shown</div></div><div class="stat"><div class="num">'+games.length+'</div><div class="lbl">Games tracked</div></div><div class="stat"><div class="num">'+wins.toLocaleString()+'</div><div class="lbl">Draws with winners</div></div><div class="stat"><div class="num">₱'+(totalJackpot/1e9).toFixed(2)+'B</div><div class="lbl">Total jackpots shown</div></div>';
}
document.querySelectorAll('table.main th[data-k]').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=1;}render();});
[gf,yf].forEach(e=>e.onchange=render);
document.getElementById('search').oninput=render;

// ===== Live refresh =====
const NAME_MAP={'Ultra Lotto 6/58':'Ultra Lotto 6/58','Grand Lotto 6/55':'Grand Lotto 6/55','Superlotto 6/49':'Super Lotto 6/49','Megalotto 6/45':'Mega Lotto 6/45','Lotto 6/42':'Lotto 6/42','4D Lotto':'4D Lotto','6D Lotto':'6D Lotto','4Digit Game':'4D Lotto','6Digit Game':'6D Lotto'};
document.getElementById('refreshBtn').onclick=async function(){
  const st=document.getElementById('status');st.textContent='Fetching latest from PCSO…';
  try{
    const url='https://www.pcso.gov.ph/searchlottoresult.aspx';
    const r0=await fetch(url);const d0=new DOMParser().parseFromString(await r0.text(),'text/html');
    const hidden={};d0.querySelectorAll('input[type=hidden]').forEach(h=>hidden[h.name]=h.value);
    const now=new Date();const months=['January','February','March','April','May','June','July','August','September','October','November','December'];
    const p=new URLSearchParams();for(const k in hidden)p.set(k,hidden[k]);
    const start=new Date(now.getTime()-30*864e5);
    p.set('ctl00$ctl00$cphContainer$cpContent$ddlStartMonth',months[start.getMonth()]);
    p.set('ctl00$ctl00$cphContainer$cpContent$ddlStartDate',String(start.getDate()));
    p.set('ctl00$ctl00$cphContainer$cpContent$ddlStartYear',String(start.getFullYear()));
    p.set('ctl00$ctl00$cphContainer$cpContent$ddlEndMonth',months[now.getMonth()]);
    p.set('ctl00$ctl00$cphContainer$cpContent$ddlEndDay',String(now.getDate()));
    p.set('ctl00$ctl00$cphContainer$cpContent$ddlEndYear',String(now.getFullYear()));
    p.set('ctl00$ctl00$cphContainer$cpContent$ddlSelectGame','0');
    p.set('ctl00$ctl00$cphContainer$cpContent$btnSearch','Search Lotto');
    const r1=await fetch(url,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:p.toString()});
    const d1=new DOMParser().parseFromString(await r1.text(),'text/html');
    let added=0;const have=new Set(DATA.map(r=>r.game+'|'+r.date+'|'+r.combination));
    function iso(d){const a=d.split('/').map(Number);return a[2]+'-'+String(a[0]).padStart(2,'0')+'-'+String(a[1]).padStart(2,'0');}
    d1.querySelectorAll('table tr').forEach(tr=>{const c=Array.from(tr.querySelectorAll('td')).map(x=>x.textContent.trim());
      if(c.length>=5 && NAME_MAP[c[0]] && /\d{1,2}\/\d{1,2}\/\d{4}/.test(c[2])){
        const rec={game:NAME_MAP[c[0]],date:iso(c[2]),combination:c[1],jackpot:c[3],winners:c[4]};
        const key=rec.game+'|'+rec.date+'|'+rec.combination;
        if(!have.has(key)){have.add(key);rec.jackpotNum=num(rec.jackpot);rec.winnersNum=parseInt(rec.winners)||0;DATA.push(rec);added++;}
      }});
    st.textContent=added+' new draw(s) added (session only — run the update script to save).';
    render();
  }catch(e){st.textContent='Live refresh blocked by browser (CORS). Use the Python update script.';}
};
render();
</script>
</body>
</html>'''


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
