"""
OOTP Luck Dashboard — data pipeline.

Takes a StatsPlus league's API token + reports URL and produces
the GM-level luck statistics JSON that the dashboard HTML consumes.
"""

from __future__ import annotations

import re
import math
import io
import csv
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import urlparse

import requests

# ─────────────────────────────────────────────────────────────
# URL parsing
# ─────────────────────────────────────────────────────────────

def parse_reports_url(url: str) -> tuple[str, str]:
    """
    Given any StatsPlus reports URL, return (subdomain_host, league_slug).
    e.g. https://atl-01.statsplus.net/skylinebaseball/reports/...
         → ('atl-01.statsplus.net', 'skylinebaseball')
    """
    parsed = urlparse(url)
    host = parsed.netloc  # e.g. 'atl-01.statsplus.net'
    # slug is the first path component
    slug = parsed.path.lstrip("/").split("/")[0]
    return host, slug


# ─────────────────────────────────────────────────────────────
# API helpers
# ─────────────────────────────────────────────────────────────

API_BASE = "https://statsplus.net/{slug}/api/{endpoint}"
REPORTS_BASE = "https://{host}/{slug}/reports/news/html/{path}"
MODERN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/157.0.0.0 Safari/537.36"
)


def _api_get(slug: str, token: str, endpoint: str, stream: bool = False):
    url = API_BASE.format(slug=slug, endpoint=endpoint)
    r = requests.get(url, params={"token": token}, allow_redirects=True,
                     stream=stream, timeout=120)
    r.raise_for_status()
    return r


def fetch_teams(slug: str, token: str) -> dict:
    """Return {team_id: {abbr, name, level}} for MLB-level teams."""
    data = _api_get(slug, token, "lgdata").json()
    return {
        t["team_id"]: {"abbr": t["abbr"], "name": t["name"], "level": t["level"]}
        for t in data.get("teams", [])
        if t.get("level") == 1
    }


def fetch_game_history(slug: str, token: str, progress_cb=None) -> list[dict]:
    """
    Download and parse the full game history CSV.
    Returns list of {game_id, home_team, away_team, date, game_type, runs_home, runs_away}.
    progress_cb(fraction) called periodically if provided.
    """
    r = _api_get(slug, token, "gamehistory", stream=True)
    total = int(r.headers.get("content-length", 0))
    buf = io.BytesIO()
    downloaded = 0
    for chunk in r.iter_content(chunk_size=65536):
        buf.write(chunk)
        downloaded += len(chunk)
        if progress_cb and total:
            progress_cb(min(downloaded / total, 1.0))
    buf.seek(0)
    text = buf.read().decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


# ─────────────────────────────────────────────────────────────
# Series reconstruction
# ─────────────────────────────────────────────────────────────

ROUND_NAMES_BY_TOTAL = {
    4: ["Wild Card", "Division Series", "Championship Series", "World Series"],
    3: ["Division Series", "Championship Series", "World Series"],
    2: ["Championship Series", "World Series"],
    1: ["World Series"],
}


def reconstruct_series(games: list[dict]) -> list[dict]:
    """
    Group playoff games into series and assign round numbers.
    Returns list of series dicts.
    """
    # Filter to playoffs only
    playoff_games = [
        g for g in games
        if str(g.get("game_type")) == "3"
    ]

    # Group by year → (team_a, team_b) sorted
    year_matchups: dict[int, dict] = defaultdict(lambda: defaultdict(list))
    for g in playoff_games:
        year = int(str(g["date"])[:4])
        home = int(g["home_team"])
        away = int(g["away_team"])
        key = tuple(sorted([home, away]))
        year_matchups[year][key].append(g)

    all_series = []
    for year, matchups in sorted(year_matchups.items()):
        raw_series = []
        for (ta, tb), gs in matchups.items():
            gs_sorted = sorted(gs, key=lambda g: g["date"])
            wins_a = sum(
                1 for g in gs_sorted
                if (int(g["home_team"]) == ta and int(g["runs1"]) > int(g["runs0"]))
                or (int(g["away_team"]) == ta and int(g["runs0"]) > int(g["runs1"]))
            )
            wins_b = len(gs_sorted) - wins_a
            winner = ta if wins_a > wins_b else tb
            loser = tb if winner == ta else ta
            raw_series.append({
                "year": year,
                "team_a": ta,
                "team_b": tb,
                "wins_a": wins_a,
                "wins_b": wins_b,
                "winner": winner,
                "loser": loser,
                "n_games": len(gs_sorted),
                "start": gs_sorted[0]["date"],
                "end": gs_sorted[-1]["date"],
            })

        # Assign rounds by temporal grouping
        raw_series.sort(key=lambda s: s["start"])
        rounds: list[list] = []
        for s in raw_series:
            placed = False
            for rnd in rounds:
                latest_end = max(r["end"] for r in rnd)
                if s["start"] > latest_end:
                    # New round starts after this round's last game
                    continue
                # Overlaps with this round's window — same round
                rnd.append(s)
                placed = True
                break
            if not placed:
                rounds.append([s])

        # Re-check: a series might bridge two groups due to ordering —
        # simpler re-pass: sort by start, group by detecting gap
        # (above is good enough for OOTP's sequential round structure)
        n_rounds = len(rounds)
        names = ROUND_NAMES_BY_TOTAL.get(n_rounds, [f"Round {i+1}" for i in range(n_rounds)])

        for rnd_idx, rnd in enumerate(rounds):
            for s in rnd:
                s["round"] = rnd_idx + 1
                s["round_name"] = names[rnd_idx] if rnd_idx < len(names) else f"Round {rnd_idx+1}"
                all_series.append(s)

    return all_series


# ─────────────────────────────────────────────────────────────
# Pythagorean win%
# ─────────────────────────────────────────────────────────────

PYTH_EXP = 1.83


def compute_pythag(games: list[dict], team_ids: set) -> dict:
    """
    Returns {(year, team_id): pythagorean_win_pct} for regular-season games only.
    """
    rs: dict = defaultdict(int)
    ra: dict = defaultdict(int)
    for g in games:
        if str(g.get("game_type")) != "0":  # regular season only
            continue
        year = int(str(g["date"])[:4])
        home = int(g["home_team"])
        away = int(g["away_team"])
        rh = int(g["runs1"])
        ra_ = int(g["runs0"])
        if home in team_ids:
            rs[(year, home)] += rh
            ra[(year, home)] += ra_
        if away in team_ids:
            rs[(year, away)] += ra_
            ra[(year, away)] += rh

    pythag = {}
    for (year, tid), scored in rs.items():
        allowed = ra.get((year, tid), 1)
        if scored == 0 and allowed == 0:
            pythag[(year, tid)] = 0.5
        else:
            ps = scored ** PYTH_EXP
            pa = allowed ** PYTH_EXP
            pythag[(year, tid)] = ps / (ps + pa) if (ps + pa) > 0 else 0.5
    return pythag


# ─────────────────────────────────────────────────────────────
# Log5 + series probability
# ─────────────────────────────────────────────────────────────

def log5(wa: float, wb: float) -> float:
    denom = wa + wb - 2 * wa * wb
    return (wa - wa * wb) / denom if denom > 0 else 0.5


def _comb(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def series_prob(p: float, wins_needed: int = 4) -> float:
    """P(win a best-of-(2*wins_needed-1) series) given per-game win prob p."""
    total = 0.0
    max_games = 2 * wins_needed - 1
    for k in range(wins_needed, max_games + 1):
        # Win game k, having won wins_needed-1 of first k-1
        total += _comb(k - 1, wins_needed - 1) * (p ** wins_needed) * ((1 - p) ** (k - wins_needed))
    return total


def _infer_wins_needed(year_series: list[dict], round_num: int) -> int:
    """Guess wins-needed for a round from completed series in that round."""
    rnd_series = [s for s in year_series if s["round"] == round_num and s["n_games"] > 0]
    if not rnd_series:
        return 4
    max_wins = max(max(s["wins_a"], s["wins_b"]) for s in rnd_series)
    return max(max_wins, 1)


# ─────────────────────────────────────────────────────────────
# Bracket path tracer
# ─────────────────────────────────────────────────────────────

def build_bracket_path(year: int, team_id: int, all_series: list[dict],
                       pythag: dict, id2abbr: dict) -> list[dict]:
    year_series = [s for s in all_series if s["year"] == year]
    if not year_series:
        return []
    n_rounds = max(s["round"] for s in year_series)
    my_pythag = pythag.get((year, team_id), 0.5)
    path_team = team_id  # tracks who to follow for counterfactual rounds
    path = []

    for rnd in range(1, n_rounds + 1):
        wins_needed = _infer_wins_needed(year_series, rnd)
        my_series = next(
            (s for s in year_series if s["round"] == rnd
             and (s["team_a"] == team_id or s["team_b"] == team_id)), None
        )
        if my_series:
            opp = my_series["team_b"] if my_series["team_a"] == team_id else my_series["team_a"]
            won = my_series["winner"] == team_id
            opp_pythag = pythag.get((year, opp), 0.5)
            gp = log5(my_pythag, opp_pythag)
            sp = series_prob(gp, wins_needed)
            mw = my_series["wins_a"] if my_series["team_a"] == team_id else my_series["wins_b"]
            ow = my_series["wins_b"] if my_series["team_a"] == team_id else my_series["wins_a"]
            path.append({
                "round": rnd,
                "round_name": my_series["round_name"],
                "opp": id2abbr.get(opp, str(opp)),
                "source": "actual",
                "mp": round(my_pythag, 4),
                "op": round(opp_pythag, 4),
                "sp": round(sp, 4),
                "res": "WON" if won else "LOST",
                "mw": mw,
                "ow": ow,
            })
            if not won:
                path_team = opp  # follow the team that beat us
        else:
            # Counterfactual — follow path_team
            cf_series = next(
                (s for s in year_series if s["round"] == rnd
                 and (s["team_a"] == path_team or s["team_b"] == path_team)), None
            )
            if cf_series:
                cf_opp_id = cf_series["team_b"] if cf_series["team_a"] == path_team else cf_series["team_a"]
                cf_opp_pythag = pythag.get((year, cf_opp_id), 0.5)
                gp = log5(my_pythag, cf_opp_pythag)
                sp = series_prob(gp, wins_needed)
                rnd_name = cf_series["round_name"]
                path.append({
                    "round": rnd,
                    "round_name": rnd_name,
                    "opp": id2abbr.get(cf_opp_id, str(cf_opp_id)),
                    "source": "counterfactual",
                    "mp": round(my_pythag, 4),
                    "op": round(cf_opp_pythag, 4),
                    "sp": round(sp, 4),
                    "res": "",
                    "mw": 0,
                    "ow": 0,
                })
                path_team = cf_series["winner"]

    return path


# ─────────────────────────────────────────────────────────────
# GM page scraper
# ─────────────────────────────────────────────────────────────

def _fetch_gm_page(host: str, slug: str, gm_id: int, retries: int = 3) -> str | None:
    url = REPORTS_BASE.format(host=host, slug=slug, path=f"coaches/human_manager_{gm_id}.html")
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": MODERN_UA},
                             allow_redirects=True, timeout=20)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                # Rate limited — back off and retry
                wait = 2 ** attempt  # 1s, 2s, 4s
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            if attempt < retries - 1:
                time.sleep(1)
    return None


def _parse_gm_page(html: str, gm_id: int) -> dict | None:
    # Name
    m = re.search(r'class="reptitle">General Manager ([^<]+)<', html)
    if not m:
        return None
    name = re.sub(r'<[^>]+>', '', m.group(1)).strip()

    # Current team (first bold in a specific pattern)
    team_m = re.search(r'<b>([^<]+)</b>\s*General Manager', html)
    current_team = re.sub(r'<[^>]+>', '', team_m.group(1)).strip() if team_m else ""

    # History table
    table_m = re.search(r'<table[^>]*class="[^"]*data[^"]*sortable[^"]*"[^>]*>(.*?)</table>',
                        html, re.DOTALL | re.IGNORECASE)
    if not table_m:
        return None

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_m.group(1), re.DOTALL)
    history = []
    for row in rows[1:]:  # skip header
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if len(cells) < 4:
            continue
        try:
            year = int(cells[0])
        except ValueError:
            continue
        team = cells[1]
        try:
            w = int(cells[2])
            l = int(cells[3])
        except (ValueError, IndexError):
            w = l = 0
        post = cells[4] if len(cells) > 4 else ""
        made_playoffs = bool(post and post.strip() and post.strip() != "-")
        won_championship = "Won World Series" in post or "Won Championship" in post
        # Round reached: count how deep based on keywords
        round_reached = 0
        if made_playoffs:
            round_reached = 1
        for kw, rnd in [("Division Series", 2), ("Championship Series", 3),
                        ("World Series", 4), ("Won World Series", 5), ("Won Championship", 5)]:
            if kw in post:
                round_reached = max(round_reached, rnd)
        history.append({
            "year": year,
            "team": team,
            "w": w,
            "l": l,
            "made_playoffs": made_playoffs,
            "round_reached": round_reached,
            "won_championship": won_championship,
            "post_season": post,
        })

    if not history:
        return None

    return {
        "id": gm_id,
        "name": name,
        "current_team": current_team,
        "history": history,
    }


def scrape_all_gms(host: str, slug: str, progress_cb=None,
                   max_consecutive_misses: int = 10) -> list[dict]:
    """
    Scan human_manager_{N}.html from 1 upward.
    Stops after max_consecutive_misses consecutive 404s.
    """
    gms = []
    gm_id = 1
    consecutive_misses = 0
    while consecutive_misses < max_consecutive_misses:
        html = _fetch_gm_page(host, slug, gm_id)
        time.sleep(0.3)  # polite delay — avoids 429s
        if html is None:
            consecutive_misses += 1
        else:
            consecutive_misses = 0
            gm = _parse_gm_page(html, gm_id)
            if gm:
                gms.append(gm)
        if progress_cb:
            progress_cb(gm_id)
        gm_id += 1
        # Safety cap — no league has 500 GMs
        if gm_id > 500:
            break
    return gms


# ─────────────────────────────────────────────────────────────
# GM stats computation
# ─────────────────────────────────────────────────────────────

def _team_id_for_abbr(abbr_or_name: str, teams: dict) -> int | None:
    """Match a team string to a team_id. Tries abbr first, then name substring."""
    abbr_or_name = abbr_or_name.strip()
    for tid, t in teams.items():
        if t["abbr"].upper() == abbr_or_name.upper():
            return tid
    for tid, t in teams.items():
        if abbr_or_name.lower() in t["name"].lower() or t["name"].lower() in abbr_or_name.lower():
            return tid
    return None


def _detect_incomplete_years(all_series: list[dict]) -> set[int]:
    """
    Flag years where the bracket has fewer rounds than the median.
    These likely have truncated game data.
    """
    year_rounds = {}
    for s in all_series:
        y = s["year"]
        year_rounds[y] = max(year_rounds.get(y, 0), s["round"])
    if not year_rounds:
        return set()
    counts = list(year_rounds.values())
    median_rounds = sorted(counts)[len(counts) // 2]
    return {y for y, r in year_rounds.items() if r < median_rounds}


def compute_gm_stats(gms: list[dict], all_series: list[dict],
                     pythag: dict, teams: dict,
                     min_appearances: int = 3) -> dict:
    """
    Returns {str(gm_id): {...stats...}} for GMs with >= min_appearances.
    """
    id2abbr = {tid: t["abbr"] for tid, t in teams.items()}
    incomplete_years = _detect_incomplete_years(all_series)
    # Expected rounds in a "full" bracket
    year_rounds = {}
    for s in all_series:
        year_rounds[s["year"]] = max(year_rounds.get(s["year"], 0), s["round"])
    full_rounds = max(year_rounds.values()) if year_rounds else 4

    result = {}
    for gm in gms:
        playoff_years = []
        for h in gm["history"]:
            if not h["made_playoffs"]:
                continue
            year = h["year"]
            team_name = h["team"]
            # Resolve team_id
            team_id = _team_id_for_abbr(team_name, teams)
            if team_id is None:
                continue

            path = build_bracket_path(year, team_id, all_series, pythag, id2abbr)
            if not path:
                continue

            # Championship probability = product of all series probs
            champ_p = 1.0
            for r in path:
                champ_p *= r["sp"]

            # Apply correction for incomplete years
            data_incomplete = year in incomplete_years
            incomplete_note = ""
            if data_incomplete:
                rounds_in_data = year_rounds.get(year, 0)
                missing = full_rounds - rounds_in_data
                correction = 0.5 ** missing
                champ_p *= correction
                incomplete_note = (
                    f"{year} game data truncated — {missing} rounds missing "
                    f"(corrected with 50/50 estimate)"
                )

            playoff_years.append({
                "year": year,
                "team": team_name,
                "team_id": team_id,
                "round_reached": h["round_reached"],
                "won_championship": h["won_championship"],
                "champ_p": round(champ_p, 6),
                "path": path,
                "data_incomplete": data_incomplete,
                "incomplete_note": incomplete_note,
            })

        if len(playoff_years) < min_appearances:
            continue

        exp_champ = sum(py["champ_p"] for py in playoff_years)
        act_champ = sum(1 for py in playoff_years if py["won_championship"])
        p_no_champ = 1.0
        for py in playoff_years:
            p_no_champ *= (1.0 - py["champ_p"])

        result[str(gm["id"])] = {
            "id": gm["id"],
            "name": gm["name"],
            "current_team": gm["current_team"],
            "appearances": len(playoff_years),
            "act_champ": act_champ,
            "exp_champ": round(exp_champ, 4),
            "luck": round(act_champ - exp_champ, 4),
            "p_no_champ": round(p_no_champ, 6),
            "playoff_years": playoff_years,
        }

    return result


# ─────────────────────────────────────────────────────────────
# Full pipeline entry point
# ─────────────────────────────────────────────────────────────

def run_pipeline(reports_url: str, api_token: str,
                 progress: dict | None = None) -> dict:
    """
    Full pipeline. progress is a dict of Streamlit progress objects:
      {step: st.progress, status: st.empty}
    Returns the gm_stats dict ready for the dashboard.
    """
    def _status(msg: str):
        if progress and "status" in progress:
            progress["status"].info(msg)

    def _prog(val: float):
        if progress and "bar" in progress:
            progress["bar"].progress(val)

    _status("Parsing league URL…")
    host, slug = parse_reports_url(reports_url)
    _prog(0.02)

    _status(f"Fetching league structure for **{slug}**…")
    teams = fetch_teams(slug, api_token)
    _prog(0.06)

    _status(f"Downloading game history ({len(teams)} teams)…")
    games = fetch_game_history(slug, api_token,
                               progress_cb=lambda f: _prog(0.06 + f * 0.30))
    _prog(0.36)

    _status("Reconstructing playoff series…")
    all_series = reconstruct_series(games)
    _prog(0.40)

    _status("Computing Pythagorean win% for all teams…")
    pythag = compute_pythag(games, set(teams.keys()))
    _prog(0.46)

    _status("Scraping GM histories from StatsPlus reports…")
    gm_count = [0]
    def _gm_prog(n):
        gm_count[0] = n
        _status(f"Scraping GM histories… (checked {n} IDs, found {len(gms_ref[0])} GMs)")

    gms_ref = [[]]
    gms_ref[0] = scrape_all_gms(host, slug, progress_cb=_gm_prog)
    gms = gms_ref[0]
    _prog(0.78)

    _status(f"Computing bracket paths and luck stats for {len(gms)} GMs…")
    gm_stats = compute_gm_stats(gms, all_series, pythag, teams)
    _prog(0.96)

    _status(f"Done — {len(gm_stats)} GMs with ≥3 playoff appearances.")
    _prog(1.0)
    return gm_stats
