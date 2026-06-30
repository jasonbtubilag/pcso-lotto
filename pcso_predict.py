#!/usr/bin/env python3
"""
PCSO Lotto — Prediction & Scoring Engine
========================================
Runs AFTER pcso_update.py has refreshed the database.

For each game drawn on a given day it:
  1. Generates 5 number combinations via frequency-weighted statistical analysis
     (using ONLY draws that happened before that date — no look-ahead).
  2. Logs them to predictions_log.json with the target draw date.
  3. Scores any previously-logged prediction whose actual result is now in the DB,
     recording how many of the 6 numbers each combo matched (0-6) and the prize
     tier reached (3+/4+/5+/6).
  4. Re-injects an up-to-date "Most Probable Combinations" panel and a
     "Prediction Track Record" panel into pcso_lotto_results.html.

Designed to be idempotent: safe to run multiple times per day.

Usage:
    python pcso_predict.py            # uses today's date
    python pcso_predict.py 2026-06-21 # force a specific date (testing)
"""
import csv, json, os, sys, math, random, collections, datetime, re
try:
    from zoneinfo import ZoneInfo
    MANILA_TZ = ZoneInfo("Asia/Manila")
except Exception:  # pragma: no cover - fallback if tzdata is unavailable
    MANILA_TZ = datetime.timezone(datetime.timedelta(hours=8))  # PHT is UTC+8, no DST


def manila_today():
    """Today's date in Manila (PCSO draw timezone), independent of the host
    clock — GitHub Actions runners are UTC, so date.today() there can be a day
    behind the actual Manila draw date for early-morning Manila runs."""
    return datetime.datetime.now(MANILA_TZ).date().isoformat()

HERE = os.path.dirname(os.path.abspath(__file__))
CSV  = os.path.join(HERE, "pcso_lotto_results.csv")
JSON = os.path.join(HERE, "pcso_lotto_results.json")
HTML = os.path.join(HERE, "pcso_lotto_results.html")
LOG  = os.path.join(HERE, "predictions_log.json")
BACK = os.path.join(HERE, "backtest_summary.json")
JACKPOT_FILE = os.path.join(HERE, "current_jackpots.json")

GAMES = {  # 6/N set games: pick 6 distinct numbers from 1..N, order-independent
    "Ultra Lotto 6/58": 58,
    "Grand Lotto 6/55": 55,
    "Super Lotto 6/49": 49,
    "Mega Lotto 6/45":  45,
    "Lotto 6/42":       42,
}
DIGIT_GAMES = {  # positional digit games: each position is a digit 0-9, ORDER MATTERS,
    "4D Lotto": 4,  # digits may repeat. Win by matching the LAST digits (4D) or the
    "6D Lotto": 6,  # FIRST-or-LAST digits (6D), in exact order.
}
# 0 = Sunday ... 6 = Saturday
SCHEDULE = {
    "Ultra Lotto 6/58": [0, 2, 5],
    "Grand Lotto 6/55": [1, 3, 6],
    "Super Lotto 6/49": [0, 2, 4],
    "Mega Lotto 6/45":  [1, 3, 5],
    "Lotto 6/42":       [2, 4, 6],
    "4D Lotto":         [1, 3, 5],   # Mon, Wed, Fri
    "6D Lotto":         [2, 4, 6],   # Tue, Thu, Sat
}
COLORS = {'Ultra Lotto 6/58':'#a855f7','Grand Lotto 6/55':'#ef4444',
          'Super Lotto 6/49':'#3b82f6','Mega Lotto 6/45':'#10b981','Lotto 6/42':'#f59e0b',
          '4D Lotto':'#14b8a6','6D Lotto':'#ec4899'}

ODDS = {  # 1 in X to hit the top prize
    **{g: math.comb(N, 6) for g, N in GAMES.items()},   # 6/N jackpot = C(N,6)
    "4D Lotto": 10000,      # all 4 digits exact = 10^4
    "6D Lotto": 1000000,    # all 6 digits exact = 10^6
}

ALL_GAMES = list(GAMES) + list(DIGIT_GAMES)


def is_digit(game):
    return game in DIGIT_GAMES


def positions(game):
    """Number of slots in a combo: 6 for 6/N lotto, 4/6 for the digit games."""
    return DIGIT_GAMES.get(game, 6)

# Official PCSO lower-tier prizes (PHP). 3-number prize is FIXED at ₱20;
# 4- and 5-number prizes are pari-mutuel "up to" maximums (they vary per draw
# with sales and number of winners). A 6/6 match wins that draw's actual
# jackpot (read from the result data). Source: PCSO prize payout chart.
PRIZES = {
    "Ultra Lotto 6/58": {3: 20, 4: 3800, 5: 280000},
    "Grand Lotto 6/55": {3: 20, 4: 3000, 5: 200000},
    "Super Lotto 6/49": {3: 20, 4: 2000, 5: 70000},
    "Mega Lotto 6/45":  {3: 20, 4: 1500, 5: 50000},
    "Lotto 6/42":       {3: 20, 4: 1000, 5: 25000},
}

# Digit-game prizes (PHP). Source: PCSO 4D/6D prize charts.
# 6D: 6 exact = rolling jackpot (read from data); 5/4/3/2 matched from the FIRST
#     or LAST end pay these FIXED amounts.
# 4D: matched from the LAST end. 4 exact = ₱10,000 minimum guaranteed (pari-mutuel
#     top tier); last 3 = ₱800; last 2 = ₱100.
PRIZES_6D = {5: 40000, 4: 4000, 3: 400, 2: 40}
PRIZES_4D = {4: 10000, 3: 800, 2: 100}


def _parse_peso(s):
    """Pull an integer peso value out of a string like '₱5,404,454.28' -> 5404454."""
    if not s:
        return 0
    digits = re.sub(r"[^0-9.]", "", str(s))
    try:
        return int(round(float(digits)))
    except (ValueError, TypeError):
        return 0


def peso(n):
    """Format a number (or a string like '128,000,000.00') as a PHP amount,
    e.g. 280000 -> '₱280,000'."""
    try:
        if isinstance(n, str):
            n = re.sub(r"[^0-9.]", "", n) or 0
        return "₱" + f"{int(round(float(n))):,}"
    except (ValueError, TypeError):
        return "₱0"


def prize_amount(game, matches, jackpot=None):
    """Return (peso_value:int, label:str) for a match level, or (0, '') if no prize.
    `jackpot` is the draw's actual jackpot string, used for a top-tier hit.
    Dispatches by game family (6/N lotto, 6D, 4D)."""
    if game == "6D Lotto":
        if matches >= 6:
            return _parse_peso(jackpot), "JACKPOT"
        p = PRIZES_6D.get(matches)
        return (p, "fixed") if p else (0, "")
    if game == "4D Lotto":
        if matches >= 4:
            return 10000, "MGA"   # pari-mutuel; minimum guaranteed per winning ticket
        p = PRIZES_4D.get(matches)
        return (p, "fixed") if p else (0, "")
    # ---- 6/N lotto ----
    if matches >= 6:
        return _parse_peso(jackpot), "JACKPOT"
    p = PRIZES.get(game, {}).get(matches)
    if not p:
        return 0, ""
    # 3-number prize is fixed; 4/5 are pari-mutuel maximums.
    return p, ("fixed" if matches == 3 else "up to")


# ---------- data ----------
def load_draws():
    """Return {game: [(date, [nums]), ...]} sorted ascending by date."""
    rows = collections.defaultdict(list)
    src = JSON if os.path.exists(JSON) else None
    if src:
        for r in json.load(open(JSON)):
            nums = [int(x) for x in r["combination"].split("-")]
            rows[r["game"]].append((r["date"], nums))
    else:
        with open(CSV) as f:
            for r in csv.DictReader(f):
                nums = [int(x) for x in r["Winning Combination"].split("-")]
                rows[r["Game"]].append((r["Draw Date"], nums))
    for g in rows:
        rows[g].sort(key=lambda x: x[0])
    return rows


def load_jackpots():
    """Return {(game, date): jackpot_string} so a 6/6 hit can show the real prize."""
    jp = {}
    if os.path.exists(JSON):
        for r in json.load(open(JSON)):
            jp[(r["game"], r["date"])] = r.get("jackpot", "")
    elif os.path.exists(CSV):
        with open(CSV) as f:
            for r in csv.DictReader(f):
                jp[(r["Game"], r["Draw Date"])] = r.get("Jackpot (PHP)", "")
    return jp


def upcoming_jackpot(game, jdb):
    """The jackpot 'to be won' today for `game`: prefer the scraped current
    jackpot (current_jackpots.json), else fall back to the most recent jackpot
    recorded in the database. Returns a peso string like '₱128,000,000' or ''."""
    cur = ""
    if os.path.exists(JACKPOT_FILE):
        try:
            cur = json.load(open(JACKPOT_FILE)).get("jackpots", {}).get(game, "")
        except Exception:
            cur = ""
    if not cur:  # fallback: latest jackpot for this game in the DB
        cand = [(d, v) for (g, d), v in jdb.items() if g == game and v]
        cur = max(cand)[1] if cand else ""
    return peso(cur) if cur else ""


def pct(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(p * len(sorted_vals)))]


def valid(combo, N, smin, smax):
    s = sum(combo)
    if not (smin <= s <= smax):
        return False
    o = sum(1 for x in combo if x % 2)
    if o < 2 or o > 4:
        return False
    l = sum(1 for x in combo if x <= N // 2)
    if l < 2 or l > 4:
        return False
    return True


PHI_FRAC = (5 ** 0.5 - 1) / 2  # 0.6180339887... = 1/phi, the golden-ratio conjugate


def golden_control(N, k=6):
    """k numbers in 1..N placed by the golden-ratio low-discrepancy sequence:
    successive multiples of phi's fractional part (0.618...) mapped onto the
    range. Phi is the "most irrational" number, so this spreads the picks as
    evenly and non-clustering as mathematically possible.
    A FIXED, deterministic combination — independent of any draw history.
    Included as a negative control: because lotto draws are uniform and
    memoryless, an arbitrary deterministic pattern must converge on the
    hypergeometric baseline (expected matches = 36/N). If it ever beat the
    statistical strategies over a long backtest, that would be noise, not skill.
    Unlike the first-k Fibonacci numbers, this scales with N, so every game
    gets its own distinct line."""
    seen = []
    i = 1
    while len(seen) < k and i <= 10 * N:  # guard; collisions are rare for PCSO N
        n = int(((i * PHI_FRAC) % 1.0) * N) + 1
        if n not in seen:
            seen.append(n)
        i += 1
    # pad in the rare case N is tiny (never happens for PCSO games, but be safe)
    n = 1
    while len(seen) < k:
        if n not in seen:
            seen.append(n)
        n += 1
    return sorted(seen[:k])


def predict(game, history):
    """history = list of [nums] from draws strictly BEFORE the target date."""
    N = GAMES[game]
    freq = collections.Counter()
    sums = []
    for nums in history:
        freq.update(nums)
        sums.append(sum(nums))
    nums_all = list(range(1, N + 1))
    # ensure every number has a baseline weight
    w = [freq[n] + 1 for n in nums_all]
    sums_sorted = sorted(sums) if sums else [N * 3]
    smin, smax = pct(sums_sorted, 0.15), pct(sums_sorted, 0.85)

    hot = sorted(nums_all, key=lambda n: (-freq[n], n))
    setA = sorted(hot[:6])

    # most overdue among the top-half-by-frequency
    last_idx = {}
    for i, nums in enumerate(history):
        for n in nums:
            last_idx[n] = i
    nlast = len(history) - 1
    due = {n: nlast - last_idx.get(n, -1) for n in nums_all}
    top_half = set(hot[:max(6, N // 2)])
    overdue = sorted(top_half, key=lambda n: (-due[n], n))
    setB = sorted(set(hot[:3] + overdue[:3]))
    for n in hot:
        if len(setB) >= 6:
            break
        if n not in setB:
            setB.append(n)
    setB = sorted(setB[:6])

    rng = random.Random()
    rng.seed(f"{game}|{len(history)}")  # deterministic per (game, history length)
    weighted = []
    guard = 0
    while len(weighted) < 3 and guard < 5000:
        guard += 1
        pool, wt, pick = nums_all[:], w[:], []
        for _ in range(6):
            t = rng.choices(range(len(pool)), weights=wt)[0]
            pick.append(pool.pop(t)); wt.pop(t)
        pick = sorted(pick)
        if valid(pick, N, smin, smax) and pick not in weighted and pick not in (setA, setB):
            weighted.append(pick)
    while len(weighted) < 3:  # fallback if filters too strict
        weighted.append(sorted(rng.sample(nums_all, 6)))

    return {
        "Frequency Leaders": setA,
        "Hot + Overdue Balance": setB,
        "Weighted Pick 1": weighted[0],
        "Weighted Pick 2": weighted[1],
        "Weighted Pick 3": weighted[2],
        "Golden Ratio (control)": golden_control(N),
    }


def golden_digit_control(P):
    """Fixed golden-ratio digit string for a P-position digit game: the i-th
    digit is floor(frac(i * 0.618...) * 10), a 0-9 digit. Deterministic — the
    digit-game analogue of golden_control(), included as a negative control."""
    return [int(((i * PHI_FRAC) % 1.0) * 10) for i in range(1, P + 1)]


def predict_digit(game, history):
    """Positional-digit predictor. history = list of ordered digit-lists from draws
    strictly BEFORE the target date. Builds combos from PER-POSITION digit statistics
    (order matters here, unlike the 6/N games)."""
    P = DIGIT_GAMES[game]
    pos_freq = [collections.Counter() for _ in range(P)]
    pos_last = [dict() for _ in range(P)]
    for i, nums in enumerate(history):
        if len(nums) != P:
            continue
        for p in range(P):
            pos_freq[p][nums[p]] = pos_freq[p].get(nums[p], 0) + 1
            pos_last[p][nums[p]] = i
    n = len(history)

    def most_freq(p):
        c = pos_freq[p]
        return max(range(10), key=lambda d: (c.get(d, 0), -d))

    def most_overdue(p):
        lst = pos_last[p]
        if not lst:
            return most_freq(p)
        c = pos_freq[p]
        return max(range(10), key=lambda d: (n - 1 - lst.get(d, -1), c.get(d, 0), -d))

    setA = [most_freq(p) for p in range(P)]                                  # per-position hottest digit
    setB = [most_freq(p) if p % 2 == 0 else most_overdue(p) for p in range(P)]  # hot/overdue mix

    rng = random.Random()
    rng.seed(f"{game}|{n}")  # deterministic per (game, history length)

    def weighted_pick():
        return [rng.choices(range(10),
                            weights=[pos_freq[p].get(d, 0) + 1 for d in range(10)])[0]
                for p in range(P)]

    weighted = [weighted_pick() for _ in range(3)]
    return {
        "Frequency Leaders": setA,
        "Hot + Overdue Balance": setB,
        "Weighted Pick 1": weighted[0],
        "Weighted Pick 2": weighted[1],
        "Weighted Pick 3": weighted[2],
        "Golden Ratio (control)": golden_digit_control(P),
    }


def predict_any(game, history):
    """Dispatch to the right predictor for the game family."""
    return predict_digit(game, history) if is_digit(game) else predict(game, history)


def _run_front(pred, actual):
    k = 0
    for a, b in zip(pred, actual):
        if a == b:
            k += 1
        else:
            break
    return k


def _run_back(pred, actual):
    return _run_front(list(reversed(pred)), list(reversed(actual)))


def digit_match_count(pred, actual):
    """Total exact-position digit matches (order-sensitive). Random expectation = P/10."""
    return sum(1 for a, b in zip(pred, actual) if a == b)


def score_combo(pred, actual, game=None):
    """Prize-determining 'match level' for a combo vs the actual result.
      - 6/N lotto: count of shared numbers (order-independent).
      - 6D: longest exact-position run from the FIRST or LAST end.
      - 4D: longest exact-position run from the LAST end."""
    if game == "6D Lotto":
        return max(_run_front(pred, actual), _run_back(pred, actual))
    if game == "4D Lotto":
        return _run_back(pred, actual)
    return len(set(pred) & set(actual))


def tier(m):
    return f"{m}/6" if m >= 3 else None


# ---------- prediction log ----------
def run_predictions(rows, today):
    log = json.load(open(LOG)) if os.path.exists(LOG) else {}
    dow = datetime.date.fromisoformat(today).weekday()
    # python weekday(): Mon=0..Sun=6  ->  convert to our Sun=0..Sat=6
    our_dow = (dow + 1) % 7
    todays_games = [g for g in ALL_GAMES if our_dow in SCHEDULE[g]]

    # 1) create today's predictions (if missing)
    for g in todays_games:
        log.setdefault(g, [])
        if any(e["draw_date"] == today for e in log[g]):
            continue
        history = [nums for d, nums in rows[g] if d < today]
        combos = predict_any(g, history)
        log[g].append({
            "draw_date": today,
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "history_size": len(history),
            "combos": combos,
            "actual": None,
            "scores": None,
        })

    # 2) score any pending entry whose result now exists
    jackpots = load_jackpots()
    for g in ALL_GAMES:
        if g not in log:
            continue
        actuals = {d: nums for d, nums in rows[g]}
        for e in log[g]:
            if e["actual"] is None and e["draw_date"] in actuals:
                e["actual"] = actuals[e["draw_date"]]
                e["scores"] = {lab: score_combo(cb, e["actual"], g)
                               for lab, cb in e["combos"].items()}
            # (Re)attach jackpot + per-combo prizes for any scored entry.
            if e["actual"] is not None:
                e["jackpot"] = jackpots.get((g, e["draw_date"]), e.get("jackpot", ""))
                e["prizes"] = {}
                for lab, m in e["scores"].items():
                    val, kind = prize_amount(g, m, e["jackpot"])
                    if val or kind:
                        e["prizes"][lab] = {"matches": m, "amount": val, "kind": kind}

    json.dump(log, open(LOG, "w"), indent=2)
    return log, todays_games


# ---------- backtest ----------
def _backtest_lotto(g, N, draws, window):
    """Walk-forward backtest for a 6/N set game vs the hypergeometric baseline."""
    start = max(30, len(draws) - window)
    per_combo = collections.defaultdict(lambda: {"draws": 0, "sum_m": 0,
                                                  "t3": 0, "t4": 0, "t5": 0, "t6": 0})
    overall = {"draws": 0, "sum_best": 0, "t3": 0, "t4": 0, "t5": 0, "t6": 0}
    for i in range(start, len(draws)):
        history = [nums for _, nums in draws[:i]]
        actual = draws[i][1]
        combos = predict(g, history)
        best = 0
        for lab, cb in combos.items():
            m = score_combo(cb, actual, g)
            c = per_combo[lab]
            c["draws"] += 1; c["sum_m"] += m
            if m >= 3: c["t3"] += 1
            if m >= 4: c["t4"] += 1
            if m >= 5: c["t5"] += 1
            if m == 6: c["t6"] += 1
            best = max(best, m)
        overall["draws"] += 1; overall["sum_best"] += best
        if best >= 3: overall["t3"] += 1
        if best >= 4: overall["t4"] += 1
        if best >= 5: overall["t5"] += 1
        if best == 6: overall["t6"] += 1
    # hypergeometric baseline: drawing 6 random from N, expected matches with the 6 winners
    exp_match = 6 * 6 / N
    p3 = sum(math.comb(6, k) * math.comb(N - 6, 6 - k) for k in range(3, 7)) / math.comb(N, 6)
    return {
        "type": "lotto", "N": N, "tested_draws": overall["draws"],
        "avg_best_matches": overall["sum_best"] / max(1, overall["draws"]),
        "tier3_draws": overall["t3"], "tier4_draws": overall["t4"],
        "tier5_draws": overall["t5"], "tier6_draws": overall["t6"],
        "per_combo": {lab: {
            "avg_matches": c["sum_m"] / max(1, c["draws"]),
            "t3": c["t3"], "t4": c["t4"], "t5": c["t5"], "t6": c["t6"],
            "draws": c["draws"],
        } for lab, c in per_combo.items()},
        "baseline_avg_matches_per_combo": exp_match,
        "baseline_p3_per_combo": p3,
        "baseline_tier3_draws_5combos": overall["draws"] * (1 - (1 - p3) ** 5),
    }


def _backtest_digit(g, P, draws, window):
    """Walk-forward backtest for a positional-digit game. Headline metric is the
    avg count of exact-position digit matches (random expectation = P/10). Prize
    tiers use the run-length scoring (first/last exact run)."""
    start = max(30, len(draws) - window)
    tiers = list(range(2, P + 1))
    per_combo = collections.defaultdict(lambda: {"draws": 0, "sum_m": 0})
    overall = {"draws": 0, "sum_best": 0}
    tier_draws = {k: 0 for k in tiers}
    for i in range(start, len(draws)):
        history = [nums for _, nums in draws[:i]]
        actual = draws[i][1]
        if len(actual) != P:
            continue
        combos = predict_digit(g, history)
        best_match = 0          # best exact-position match count (headline metric)
        best_tier = 0           # best prize tier (run length) for this draw
        for lab, cb in combos.items():
            mc = digit_match_count(cb, actual)
            c = per_combo[lab]
            c["draws"] += 1; c["sum_m"] += mc
            best_match = max(best_match, mc)
            best_tier = max(best_tier, score_combo(cb, actual, g))
        overall["draws"] += 1; overall["sum_best"] += best_match
        for k in tiers:
            if best_tier >= k:
                tier_draws[k] += 1
    # random baseline: each position uniform 0-9, independent -> P*(1/10) expected matches
    exp_match = P / 10.0
    return {
        "type": "digit", "positions": P, "tested_draws": overall["draws"],
        "avg_best_matches": overall["sum_best"] / max(1, overall["draws"]),
        "baseline_avg_matches_per_combo": exp_match,
        "tier_draws": {str(k): tier_draws[k] for k in tiers},
        "per_combo": {lab: {
            "avg_matches": c["sum_m"] / max(1, c["draws"]),
            "draws": c["draws"],
        } for lab, c in per_combo.items()},
    }


def backtest(rows, window=300):
    """For each of the last `window` draws per game, predict from prior data and
    score it, then compare to the appropriate random baseline (hypergeometric for
    6/N set games, uniform-per-position for the digit games)."""
    summary = {}
    for g in ALL_GAMES:
        draws = rows.get(g, [])
        if len(draws) < 60:
            continue
        if is_digit(g):
            summary[g] = _backtest_digit(g, DIGIT_GAMES[g], draws, window)
        else:
            summary[g] = _backtest_lotto(g, GAMES[g], draws, window)
    json.dump(summary, open(BACK, "w"), indent=2)
    return summary


# ---------- HTML rendering ----------
def balls(cb, col, dim=False, width=2):
    op = ";opacity:.45" if dim else ""
    return "".join(
        f'<span class="pball" style="background:{col}{op};">{x:0{width}d}</span>' for x in cb)


_GOLD = ("background:linear-gradient(145deg,#fde047,#f59e0b);color:#1a1300;"
         "box-shadow:0 0 0 2px #fffbeb,0 0 10px rgba(250,204,21,.7);font-weight:800;")
_MISS = "background:rgba(255,255,255,.08);color:#94a3b8;"


def hit_balls(cb, actual, col, width=2):
    """6/N set game: matched numbers (anywhere in the draw) turn GOLD, misses dim."""
    aset = set(actual)
    return "".join(
        f'<span class="pball" style="{_GOLD if x in aset else _MISS}">{x:0{width}d}</span>'
        for x in cb)


def hit_balls_digit(cb, actual, game):
    """Digit game: a position turns GOLD only if it is part of the winning exact-order
    run that earns a prize — the LAST-end run for 4D, the FIRST-or-LAST run for 6D."""
    P = len(cb)
    b = _run_back(cb, actual)
    win = set(range(P - b, P))
    if game == "6D Lotto":
        win |= set(range(_run_front(cb, actual)))
    return "".join(
        f'<span class="pball" style="{_GOLD if p in win else _MISS}">{x:01d}</span>'
        for p, x in enumerate(cb))


def render_predictions(log, todays_games, today):
    if not todays_games:
        inner = '<div class="pnone">No major lotto draws scheduled for today.</div>'
    else:
        jdb = load_jackpots()
        cards = ""
        for g in todays_games:
            col = COLORS[g]
            dg = is_digit(g)
            P = positions(g)
            # 6D carries a rolling jackpot; 4D does not, so only show the banner for 6D/lotto.
            jp_today = "" if g == "4D Lotto" else upcoming_jackpot(g, jdb)
            entry = next((e for e in log[g] if e["draw_date"] == today), None)
            if not entry:
                continue
            rows_html = ""
            card_total = 0
            for lab, cb in entry["combos"].items():
                if entry["actual"]:
                    m = entry["scores"][lab]
                    bb = (hit_balls_digit(cb, entry["actual"], g) if dg
                          else hit_balls(cb, entry["actual"], col))
                    val, kind = prize_amount(g, m, entry.get("jackpot"))
                    if kind == "JACKPOT":
                        prize = f'<span class="pprize jackpot">🏆 JACKPOT {peso(val)}</span>'
                        card_total += val
                    elif kind:
                        pre = "up to " if kind == "up to" else ""
                        prize = f'<span class="pprize">{pre}{peso(val)}</span>'
                        card_total += val
                    else:
                        prize = '<span class="pprize none">no prize</span>'
                    unit = "in order" if dg else "matched"
                    right = (f'<div class="pmatch"><b>{m}/{P}</b> {unit}</div>{prize}')
                else:
                    bb = balls(cb, col, width=1 if dg else 2)
                    if dg:
                        note = "-".join(str(x) for x in cb)
                    else:
                        s = sum(cb); o = sum(1 for x in cb if x % 2)
                        note = f'sum {s} · {o}odd/{6-o}even'
                    right = f'<div class="pmeta">{note}</div>'
                rows_html += (f'<div class="prow"><div class="plabel">{lab}</div>'
                              f'<div class="pballs">{bb}</div>{right}</div>')
            if entry["actual"]:
                width = 1 if dg else 2
                act = "-".join(f"{x:0{width}d}" for x in entry["actual"])
                ncombos = len(entry["combos"])
                won = (f' · <b style="color:#fde047;">won {peso(card_total)}</b> '
                       f'<span style="color:#94a3b8;">(all {ncombos} combos, max)</span>'
                       if card_total else
                       ' · <span style="color:#94a3b8;">no prize tier reached</span>')
                status = (f'<div class="pfoot">✅ Actual result: '
                          f'<b style="color:#fde047;">{act}</b>{won}</div>')
            else:
                status = '<div class="pfoot">⏳ Awaiting tonight\'s 9:00 PM draw…</div>'
            jp_banner = (f'<div class="pjackpot">💰 Jackpot to be won today: '
                         f'<b>{jp_today}</b></div>') if jp_today else ''
            odds_label = "top-prize odds" if dg else "jackpot odds"
            cards += (f'<div class="pcard" style="border-top:4px solid {col};">'
                      f'<h3 style="color:{col};">🎱 {g}</h3>'
                      f'{jp_banner}'
                      f'<div class="psub">{entry["history_size"]:,} historical draws · {odds_label} 1 in {ODDS[g]:,}</div>'
                      f'{rows_html}{status}</div>')
        inner = f'<div class="pgrid">{cards}</div>'
    disc = ('⚠️ <b>Mathematician\'s note:</b> chi-square testing shows PCSO draws are consistent with a '
            '<b>fair, uniform random process</b> — every combination is equally likely, so no method '
            'truly raises your odds. The 6/N picks weight historically frequent numbers and respect typical '
            'winning structure; the 4D/6D picks weight frequent digits per position. All for entertainment only. '
            'Play responsibly.')
    return (f'<div class="predict" id="predict">'
            f'<h2>🔮 Most Probable Combinations — Today\'s Draws</h2>'
            f'<div class="pdate">{datetime.date.fromisoformat(today).strftime("%A, %B %d, %Y")} · '
            f'frequency-weighted statistical analysis</div>{inner}'
            f'<div class="pdisclaimer">{disc}</div></div>')


def _prize_threshold(game):
    """Lowest match level that earns any prize: 3 for 6/N lotto, 2 for the digit games."""
    return 2 if is_digit(game) else 3


def render_trackrecord(log, back):
    # ---- live scored results ----
    scored = []
    for g in ALL_GAMES:
        for e in log.get(g, []):
            if e["actual"]:
                scored.append((g, e))
    scored.sort(key=lambda x: x[1]["draw_date"], reverse=True)

    tot_combos = sum(len(e["combos"]) for _, e in scored)
    tot_m = sum(sum(e["scores"].values()) for _, e in scored)
    prize_hits = sum(1 for g, e in scored for v in e["scores"].values()
                     if v >= _prize_threshold(g))
    drws_w = sum(1 for g, e in scored if max(e["scores"].values()) >= _prize_threshold(g))
    live_avg = (tot_m / tot_combos) if tot_combos else 0

    # total winnings across every scored combo (pari-mutuel tiers counted at max)
    def draw_winnings(g, e):
        return sum(prize_amount(g, m, e.get("jackpot"))[0] for m in e["scores"].values())
    tot_won = sum(draw_winnings(g, e) for g, e in scored)

    if scored:
        head = (f'<div class="trstats">'
                f'<div class="trbox"><div class="trn">{len(scored)}</div><div class="trl">draws scored</div></div>'
                f'<div class="trbox"><div class="trn">{live_avg:.2f}</div><div class="trl">avg score / combo</div></div>'
                f'<div class="trbox"><div class="trn">{prize_hits}</div><div class="trl">prize hits</div></div>'
                f'<div class="trbox"><div class="trn">{drws_w}</div><div class="trl">draws w/ a winning combo</div></div>'
                f'<div class="trbox gold"><div class="trn" style="color:#fde047;">{peso(tot_won)}</div><div class="trl">total winnings (max)</div></div>'
                f'</div>')
        rows = ""
        for g, e in scored[:30]:
            col = COLORS[g]; P = positions(g); thr = _prize_threshold(g)
            width = 1 if is_digit(g) else 2
            best = max(e["scores"].values())
            chips = " ".join(
                f'<span class="trchip" style="border-color:{col};{"background:"+col+";color:#fff;" if m>=thr else ""}">{lab.split()[0][:4]} {m}</span>'
                for lab, m in e["scores"].items())
            act = "-".join(f"{x:0{width}d}" for x in e["actual"])
            w = draw_winnings(g, e)
            prize_cell = (f'<b style="color:#fde047;">{peso(w)}</b>' if w
                          else '<span style="color:#64748b;">—</span>')
            rows += (f'<tr><td>{e["draw_date"]}</td><td style="color:{col};">{g}</td>'
                     f'<td><b style="color:{"#fde047" if best>=thr else "#e2e8f0"}">{best}/{P}</b></td>'
                     f'<td>{prize_cell}</td>'
                     f'<td class="tract">{act}</td><td>{chips}</td></tr>')
        live = (head + '<div class="trscroll"><table class="trtable"><thead><tr><th>Date</th><th>Game</th>'
                '<th>Best</th><th>Prize</th><th>Actual</th><th>Per-combo score</th></tr></thead><tbody>'
                + rows + '</tbody></table></div>')
    else:
        live = ('<div class="pnone">No predictions have been scored yet. The first scores appear the '
                'night a predicted draw takes place (10 PM run).</div>')

    # ---- backtest: 6/N set games ----
    lotto_rows = ""
    for g, s in back.items():
        if s.get("type") != "lotto":
            continue
        col = COLORS[g]
        lotto_rows += (
            f'<tr><td style="color:{col};">{g}</td><td>{s["tested_draws"]}</td>'
            f'<td>{s["avg_best_matches"]:.2f}</td>'
            f'<td>{s["baseline_avg_matches_per_combo"]:.2f}</td>'
            f'<td>{s["tier3_draws"]}</td><td>{s["tier4_draws"]}</td>'
            f'<td>{s["tier5_draws"]}</td><td>{s["tier6_draws"]}</td>'
            f'<td>{s["baseline_tier3_draws_5combos"]:.1f}</td></tr>')
    lotto_table = ('<div class="trscroll"><table class="trtable"><thead><tr><th>Game</th><th>Draws tested</th>'
                   '<th>Avg best match</th><th>Random avg/combo</th>'
                   '<th>3/6</th><th>4/6</th><th>5/6</th><th>6/6</th>'
                   '<th>Exp. 3+ (random)</th></tr></thead><tbody>'
                   + lotto_rows + '</tbody></table></div>') if lotto_rows else ''

    # ---- backtest: positional-digit games ----
    digit_rows = ""
    for g, s in back.items():
        if s.get("type") != "digit":
            continue
        col = COLORS[g]; td = s["tier_draws"]
        cell = lambda k: td.get(str(k), "—") if int(k) <= s["positions"] else "—"
        digit_rows += (
            f'<tr><td style="color:{col};">{g}</td><td>{s["positions"]}</td>'
            f'<td>{s["tested_draws"]}</td>'
            f'<td>{s["avg_best_matches"]:.2f}</td>'
            f'<td>{s["baseline_avg_matches_per_combo"]:.2f}</td>'
            f'<td>{cell(2)}</td><td>{cell(3)}</td><td>{cell(4)}</td>'
            f'<td>{cell(5)}</td><td>{cell(6)}</td></tr>')
    digit_table = ('<h4 style="margin:14px 0 4px;color:#cbd5e1;">Digit games (4D / 6D)</h4>'
                   '<div class="psub">Headline metric: avg count of exact-position digit matches '
                   '(random expectation = positions ÷ 10). Tier columns count draws whose best combo '
                   'matched ≥k digits in exact order from an end (a paying tier).</div>'
                   '<div class="trscroll"><table class="trtable"><thead><tr><th>Game</th><th>Pos.</th><th>Draws tested</th>'
                   '<th>Avg best pos-match</th><th>Random avg/combo</th>'
                   '<th>2+</th><th>3+</th><th>4+</th><th>5+</th><th>6</th></tr></thead><tbody>'
                   + digit_rows + '</tbody></table></div>') if digit_rows else ''

    backtest_html = (
        '<h3 style="margin-top:18px;">📊 Backtest — strategy vs. random baseline</h3>'
        '<div class="psub">Each past draw was predicted using only the data available before it '
        '(6 combos / draw, including the golden-ratio control), then scored. "Random baseline" is the '
        'mathematical expectation of random tickets.</div>'
        + lotto_table + digit_table)

    return (f'<div class="predict trackrecord" id="trackrecord">'
            f'<h2>🎯 Prediction Track Record</h2>'
            f'<div class="pdate">How the recommended combinations have actually performed</div>'
            f'{live}{backtest_html}'
            f'<div class="pdisclaimer">The backtest typically lands within a hair of the random baseline — '
            f'the honest, expected outcome for a fair lottery. Treat any streak as luck, not skill.</div></div>')


CSS = '''  .predict{background:linear-gradient(135deg,#1e1b4b,#312e81);border-radius:12px;padding:18px 20px;margin-bottom:20px;}
  .predict.trackrecord{background:linear-gradient(135deg,#0c2a22,#134e3a);}
  .predict h2{margin:0 0 2px;font-size:1.15rem;}
  .pdate{color:#c7d2fe;font-size:.82rem;margin-bottom:14px;}
  .trackrecord .pdate{color:#a7f3d0;}
  .pgrid{display:flex;flex-direction:column;gap:16px;}
  .pcard{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12);border-radius:12px;padding:18px 24px;}
  .pcard h3{margin:0 0 2px;font-size:1.1rem;}
  .psub{color:#94a3b8;font-size:.76rem;margin-bottom:10px;}
  .pjackpot{display:inline-block;margin:2px 0 10px;font-size:.92rem;color:#fde047;background:rgba(250,204,21,.10);border:1px solid rgba(250,204,21,.35);border-radius:8px;padding:6px 12px;}
  .pjackpot b{font-size:1.05rem;color:#fef08a;}
  .prow{display:flex;align-items:center;gap:18px;padding:12px 4px;border-top:1px solid rgba(255,255,255,.08);}
  .prow:first-of-type{border-top:none;}
  .plabel{flex:0 0 170px;font-size:.82rem;color:#e2e8f0;font-weight:600;}
  .pballs{display:flex;gap:8px;flex-wrap:wrap;flex:0 0 auto;}
  .pball{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:50%;color:#fff;font-weight:700;font-size:.86rem;}
  .pmatch{flex:0 0 auto;margin-left:auto;font-size:.78rem;color:#94a3b8;white-space:nowrap;}
  .pmatch b{color:#fde047;font-size:.92rem;}
  .pmeta{flex:0 0 auto;margin-left:auto;font-size:.74rem;color:#94a3b8;white-space:nowrap;}
  .pprize{flex:0 0 auto;}
  .pprize{font-size:.7rem;font-weight:700;color:#fde047;background:rgba(250,204,21,.12);border:1px solid rgba(250,204,21,.4);border-radius:20px;padding:1px 8px;}
  .pprize.none{color:#64748b;background:none;border-color:rgba(255,255,255,.10);font-weight:500;}
  .pprize.jackpot{color:#1a1300;background:linear-gradient(145deg,#fde047,#f59e0b);border-color:#fde047;}
  .pfoot{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,.12);font-size:.78rem;color:#cbd5e1;}
  .pnone{color:#c7d2fe;font-style:italic;font-size:.85rem;}
  .pdisclaimer{margin-top:14px;font-size:.74rem;color:#e0e7ff;background:rgba(0,0,0,.25);border-radius:8px;padding:10px 12px;line-height:1.55;}
  .trstats{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:14px;}
  .trbox{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:10px 16px;min-width:120px;text-align:center;}
  .trbox.gold{background:rgba(250,204,21,.10);border-color:rgba(250,204,21,.45);}
  .trn{font-size:1.5rem;font-weight:800;color:#6ee7b7;}
  .trl{font-size:.72rem;color:#94a3b8;}
  .trtable{width:100%;border-collapse:collapse;font-size:.78rem;margin-top:6px;}
  .trtable th{text-align:left;color:#94a3b8;font-weight:600;padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.15);}
  .trtable td{padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.06);}
  .tract{font-family:monospace;color:#a7f3d0;}
  .trchip{display:inline-block;border:1px solid;border-radius:20px;padding:1px 8px;font-size:.68rem;margin:1px;color:#cbd5e1;}
  .trscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
  @media (max-width:640px){
    .predict{padding:14px 14px;}
    .pcard{padding:14px 16px;}
    .pcard h3{font-size:1rem;}
    .prow{flex-direction:column;align-items:stretch;gap:8px;padding:12px 2px;}
    .plabel{flex:0 0 auto;}
    .pballs{flex-wrap:wrap;}
    .pmatch,.pmeta,.pprize{margin-left:0;align-self:flex-start;}
    .pball{width:32px;height:32px;font-size:.8rem;}
    .pjackpot{font-size:.84rem;}
    .trbox{flex:1 1 calc(50% - 6px);min-width:0;}
    .trtable{font-size:.72rem;white-space:nowrap;}
    .trtable th,.trtable td{padding:6px 7px;}
  }
'''


def inject_html(predict_html, track_html):
    if not os.path.exists(HTML):
        print("HTML not found; skipping injection."); return
    html = open(HTML, encoding="utf-8").read()
    # remove prior injected blocks (idempotent)
    html = re.sub(r'  <div class="predict" id="predict">.*?</div>\n(?=  <div|  <div class="sched")',
                  '', html, flags=re.S)
    html = re.sub(r'<div class="predict" id="predict">.*?<div class="pdisclaimer">.*?</div></div>',
                  '', html, flags=re.S)
    html = re.sub(r'<div class="predict trackrecord" id="trackrecord">.*?<div class="pdisclaimer">.*?</div></div>',
                  '', html, flags=re.S)
    # strip any prior predict CSS block then re-add fresh
    html = re.sub(r'  \.predict\{.*?\.trchip\{[^}]*\}\n', '', html, flags=re.S)
    html = re.sub(r'  \.predict\{.*?\.pdisclaimer\{[^}]*\}\n', '', html, flags=re.S)
    html = html.replace('</style>', CSS + '</style>', 1)
    anchor = '<div class="today" id="today"></div>'
    block = '\n  ' + predict_html + '\n  ' + track_html + '\n'
    html = html.replace(anchor, anchor + block, 1)
    open(HTML, "w", encoding="utf-8").write(html)
    print("HTML updated.")


def main():
    today = sys.argv[1] if len(sys.argv) > 1 else manila_today()
    rows = load_draws()
    log, todays_games = run_predictions(rows, today)
    back = backtest(rows)
    inject_html(render_predictions(log, todays_games, today),
                render_trackrecord(log, back))
    print(f"Done for {today}. Today's games: {todays_games or 'none'}")


if __name__ == "__main__":
    main()
