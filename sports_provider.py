import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from fractions import Fraction

from team_zh import to_zh_league, to_zh_team

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_CACHE_PATH = Path(__file__).resolve().parent / "odds_cache.json"
ODDS_CACHE_SEED_PATH = Path(__file__).resolve().parent / "odds_cache_seed.json"
ODDS_MIN_INTERVAL_SEC = 1
# 手動更新最短間隔（小時），避免短時間重複扣額度；可用環境變數覆寫
ODDS_REFRESH_COOLDOWN_HOURS = max(1, int(os.getenv("ODDS_REFRESH_COOLDOWN_HOURS", "24")))

# 每個聯盟 1 次 API（regions=eu, markets=h2h,spreads,totals）= 1 credit
ODDS_API_FEEDS = [
    {"key": "baseball_mlb", "sport": "棒球", "league": "MLB"},
    {"key": "basketball_nba", "sport": "籃球", "league": "NBA"},
    {"key": "soccer_fifa_world_cup", "sport": "世界盃", "league": "世界盃"},
]

STATUS_LIVE = "進行中"
STATUS_UPCOMING = "即將開賽"
STATUS_HOT = "熱門"

FACTOR_TEMPLATES = {
    "籃球": ["近況", "球星狀態", "主場優勢", "對戰往績", "休息天數", "盤口趨勢"],
    "足球": ["近況", "天氣", "主場", "陣容完整", "進攻效率", "盤口趨勢"],
    "棒球": ["近況", "先發投手", "打線狀態", "主場", "對戰往績", "盤口趨勢"],
    "世界盃": ["近況", "戰術", "主場氛圍", "傷兵", "對戰往績", "盤口趨勢"],
}

FACTOR_COLORS = ["#fb7185", "#fbbf24", "#34d399", "#60a5fa", "#a78bfa", "#22d3ee"]

ODDS_API_META: Dict = {
    "provider": "The Odds API",
    "last_status": "尚未更新",
    "last_fetch_at": None,
    "cached_count": 0,
    "requests_remaining": None,
    "requests_used": None,
}


def get_odds_api_key() -> str:
    return os.getenv("ODDS_API_KEY", "").strip() or os.getenv("THE_ODDS_API_KEY", "").strip()


def _today_str() -> str:
    return date.today().isoformat()


def _seed_from(text: str) -> int:
    return sum(ord(c) for c in text)


def _parse_commence_time(raw: str) -> Tuple[str, str]:
    if not raw:
        return _today_str(), "00:00"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.date().isoformat(), local.strftime("%H:%M")
    except ValueError:
        return _today_str(), "00:00"


def _guess_status(game_date: str, game_time: str) -> str:
    try:
        dt = datetime.fromisoformat(f"{game_date}T{game_time}:00")
    except ValueError:
        return STATUS_UPCOMING
    now = datetime.now()
    if dt <= now <= dt + timedelta(hours=3):
        return STATUS_LIVE
    if dt - now <= timedelta(hours=6):
        return STATUS_HOT
    return STATUS_UPCOMING


def _extract_h2h_odds(event: dict, sport_category: str) -> Dict[str, float]:
    home_team = event.get("home_team") or "主隊"
    away_team = event.get("away_team") or "客隊"
    default_draw = 3.2 if sport_category in {"足球", "世界盃"} else 12.0

    for bookmaker in event.get("bookmakers") or []:
        for market in bookmaker.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            prices = {}
            for outcome in market.get("outcomes") or []:
                name = outcome.get("name")
                price = outcome.get("price")
                if name and price:
                    prices[name] = float(price)
            home = prices.get(home_team)
            away = prices.get(away_team)
            draw = prices.get("Draw") or prices.get("draw") or default_draw
            if home and away:
                return {"home": home, "draw": float(draw), "away": away}

    return {"home": 2.0, "draw": default_draw, "away": 2.0}


def _extract_spreads_odds(event: dict, home_en: str, away_en: str) -> Optional[dict]:
    """
    spreads:
      - outcomes 會帶 point（例如 -1.25、+1.25）
      - 我們只取第一組同時有主客兩邊的 spreads
    """
    for bookmaker in event.get("bookmakers") or []:
        for market in bookmaker.get("markets") or []:
            if market.get("key") != "spreads":
                continue

            home_price = None
            away_price = None
            home_point = None
            away_point = None

            for outcome in market.get("outcomes") or []:
                name = outcome.get("name")
                price = outcome.get("price")
                point = outcome.get("point")
                if name is None or price is None:
                    continue

                # price 與 point 通常都會存在，但保險起見要檢查
                price_f = float(price)
                if name == home_en:
                    home_price = price_f
                    home_point = float(point) if point is not None else None
                elif name == away_en:
                    away_price = price_f
                    away_point = float(point) if point is not None else None

            if home_price is not None and away_price is not None:
                return {
                    "home": home_price,
                    "away": away_price,
                    "homePoint": home_point,
                    "awayPoint": away_point,
                }

    return None


def _extract_totals_odds(event: dict) -> Optional[dict]:
    """
    totals:
      - outcomes 會帶 point（例如 2.25）
      - 我們只取第一組同時有 Over/Under 的 totals
    """
    for bookmaker in event.get("bookmakers") or []:
        for market in bookmaker.get("markets") or []:
            if market.get("key") != "totals":
                continue

            over_price = None
            under_price = None
            point = None

            for outcome in market.get("outcomes") or []:
                name = outcome.get("name") or ""
                price = outcome.get("price")
                p = outcome.get("point")
                if price is None:
                    continue

                price_f = float(price)
                if "Over" in name:
                    over_price = price_f
                    point = float(p) if p is not None else point
                elif "Under" in name:
                    under_price = price_f
                    point = float(p) if p is not None else point

            if over_price is not None and under_price is not None and point is not None:
                return {"over": over_price, "under": under_price, "point": point}

    return None


def _update_odds_meta_from_headers(headers: httpx.Headers, status: str):
    ODDS_API_META["last_status"] = status
    remaining = headers.get("x-requests-remaining")
    used = headers.get("x-requests-used")
    if remaining is not None:
        ODDS_API_META["requests_remaining"] = int(remaining)
    if used is not None:
        ODDS_API_META["requests_used"] = int(used)


def _parse_odds_events(events: List[dict], feed: dict) -> List[Dict]:
    rows = []
    sport_category = feed["sport"]
    league = feed["league"]

    for event in events:
        game_date, game_time = _parse_commence_time(event.get("commence_time") or "")
        event_id = event.get("id") or f"{feed['key']}-{len(rows)}"
        home_en = event.get("home_team") or "主隊"
        away_en = event.get("away_team") or "客隊"
        league_zh = to_zh_league(league)

        spreads = _extract_spreads_odds(event, home_en, away_en)
        totals = _extract_totals_odds(event)
        rows.append(
            {
                "id": f"odds-{event_id}",
                "sport": sport_category,
                "league": league_zh,
                "leagueEn": league,
                "date": game_date,
                "status": _guess_status(game_date, game_time),
                "time": game_time,
                "home": to_zh_team(home_en),
                "away": to_zh_team(away_en),
                "homeEn": home_en,
                "awayEn": away_en,
                "odds": _extract_h2h_odds(event, sport_category),
                "spreads": spreads or {},
                "totals": totals or {},
                "market": "獨贏",
                "source": "the-odds-api",
            }
        )
    return rows


def fetch_odds_for_feed(feed: dict, api_key: str) -> Tuple[List[Dict], str, httpx.Headers]:
    url = f"{ODDS_API_BASE}/sports/{feed['key']}/odds"
    params = {
        "apiKey": api_key,
        "regions": "eu",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal",
    }

    try:
        with httpx.Client(timeout=20.0) as client:
            res = client.get(url, params=params)
            if res.status_code == 401:
                return [], "API 金鑰無效，請到 the-odds-api.com 重新申請", res.headers
            if res.status_code == 422:
                payload = res.json()
                return [], payload.get("message") or "此聯盟目前無賽事", res.headers
            if res.status_code != 200:
                return [], f"HTTP {res.status_code}", res.headers
            events = res.json()
            if not isinstance(events, list):
                msg = events.get("message") if isinstance(events, dict) else "回傳格式錯誤"
                return [], msg or "回傳格式錯誤", res.headers
            return _parse_odds_events(events, feed), "OK", res.headers
    except Exception as exc:
        return [], str(exc), httpx.Headers({})


def save_odds_cache(matches: List[Dict], meta: dict):
    ODDS_CACHE_PATH.write_text(
        json.dumps(
            {
                "saved_at": datetime.now().isoformat(),
                "meta": meta,
                "matches": matches,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_cache_payload(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _apply_cache_meta(payload: dict):
    matches = payload.get("matches") or []
    meta = payload.get("meta") or {}
    ODDS_API_META["last_fetch_at"] = payload.get("saved_at")
    ODDS_API_META["cached_count"] = len(matches)
    if meta.get("status"):
        ODDS_API_META["last_status"] = meta.get("status")
    if meta.get("requests_remaining") is not None:
        ODDS_API_META["requests_remaining"] = meta.get("requests_remaining")
    if meta.get("requests_used") is not None:
        ODDS_API_META["requests_used"] = meta.get("requests_used")


def _cache_matches_valid(matches: List[Dict]) -> bool:
    if not matches:
        return False
    has_spreads = any((m.get("spreads") or {}).get("home") for m in matches if isinstance(m, dict))
    has_totals = any((m.get("totals") or {}).get("over") for m in matches if isinstance(m, dict))
    return has_spreads and has_totals


def get_cache_age_hours() -> Optional[float]:
    saved_at = ODDS_API_META.get("last_fetch_at")
    if not saved_at:
        return None
    try:
        saved_dt = datetime.fromisoformat(saved_at)
        if saved_dt.tzinfo is None:
            saved_dt = saved_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - saved_dt.astimezone(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def load_odds_cache() -> List[Dict]:
    for path in (ODDS_CACHE_PATH, ODDS_CACHE_SEED_PATH):
        payload = _read_cache_payload(path)
        if not payload:
            continue
        matches = payload.get("matches") or []
        if not _cache_matches_valid(matches):
            continue
        _apply_cache_meta(payload)
        return matches
    return []


def refresh_from_odds_api(force: bool = False) -> dict:
    api_key = get_odds_api_key()
    if not api_key:
        return {
            "ok": False,
            "matches": [],
            "count": 0,
            "source": get_data_source_label([]),
            "errors": ["請先設定 ODDS_API_KEY（到 https://the-odds-api.com 免費註冊取得）"],
            "oddsApi": get_odds_api_meta(),
        }

    cached = load_all_matches()
    age_hours = get_cache_age_hours()
    if not force and cached and age_hours is not None and age_hours < ODDS_REFRESH_COOLDOWN_HOURS:
        hours_left = max(0.1, ODDS_REFRESH_COOLDOWN_HOURS - age_hours)
        return {
            "ok": True,
            "skipped": True,
            "matches": cached,
            "count": len(cached),
            "source": get_data_source_label(cached),
            "errors": [],
            "oddsApi": get_odds_api_meta(),
            "message": (
                f"距上次更新未滿 {ODDS_REFRESH_COOLDOWN_HOURS} 小時，已沿用快取"
                f"（約 {hours_left:.1f} 小時後可再更新）"
            ),
            "creditsHint": "本次未消耗 API 額度",
        }

    all_matches: List[Dict] = []
    errors = []
    last_headers = httpx.Headers({})

    for idx, feed in enumerate(ODDS_API_FEEDS):
        if idx > 0:
            time.sleep(ODDS_MIN_INTERVAL_SEC)
        rows, status, headers = fetch_odds_for_feed(feed, api_key)
        last_headers = headers
        _update_odds_meta_from_headers(headers, status)
        if status != "OK":
            errors.append(f"{feed['league']}：{status}")
            continue
        all_matches.extend(rows)

    meta = {
        "status": ODDS_API_META["last_status"],
        "requests_remaining": ODDS_API_META.get("requests_remaining"),
        "requests_used": ODDS_API_META.get("requests_used"),
        "provider": "The Odds API",
    }

    if all_matches:
        save_odds_cache(all_matches, meta)
        ODDS_API_META["last_fetch_at"] = datetime.now().isoformat()
        ODDS_API_META["cached_count"] = len(all_matches)
    elif last_headers:
        _update_odds_meta_from_headers(last_headers, ODDS_API_META["last_status"])

    enriched = [_enrich_match(m) for m in all_matches]
    return {
        "ok": bool(all_matches),
        "skipped": False,
        "matches": enriched,
        "count": len(enriched),
        "source": get_data_source_label(enriched),
        "errors": errors,
        "oddsApi": get_odds_api_meta(),
        "creditsHint": "每次更新依聯盟計費（1 聯盟 = 1 次 API），一次回傳該聯盟所有賽事，不是一場一扣",
    }


def _enrich_match(match: Dict) -> Dict:
    row = dict(match)
    home_en = row.get("homeEn") or row.get("home") or "主隊"
    away_en = row.get("awayEn") or row.get("away") or "客隊"
    league_raw = row.get("leagueEn") or row.get("league") or "聯盟"
    row["homeEn"] = home_en
    row["awayEn"] = away_en
    row["leagueEn"] = league_raw
    row["home"] = to_zh_team(home_en)
    row["away"] = to_zh_team(away_en)
    row["league"] = to_zh_league(league_raw)
    row.setdefault("market", "獨贏")
    return row


def load_all_matches() -> List[Dict]:
    return [_enrich_match(m) for m in load_odds_cache()]


def filter_matches(
    matches: List[Dict],
    sport: str = "all",
    league: str = "all",
    day: str = "",
    statuses: Optional[List[str]] = None,
) -> List[Dict]:
    status_set = set(statuses or [])
    out = []
    for m in matches:
        if sport != "all" and m["sport"] != sport:
            continue
        if league != "all" and m["league"] != league:
            continue
        if day and m["date"] != day:
            continue
        if status_set and m["status"] not in status_set:
            continue
        out.append(m)
    return out


def list_leagues(matches: List[Dict], sport: str = "all") -> List[str]:
    leagues = set()
    for m in matches:
        if sport == "all" or m["sport"] == sport:
            leagues.add(m["league"])
    return sorted(leagues)


def odds_to_probs(odds: Dict[str, float]) -> Dict[str, float]:
    raw_home = 1 / odds["home"]
    raw_draw = 1 / odds["draw"]
    raw_away = 1 / odds["away"]
    total = raw_home + raw_draw + raw_away
    return {"home": raw_home / total, "draw": raw_draw / total, "away": raw_away / total}


def decimal_to_fractional_odds(decimal_odds: float, max_denominator: int = 20) -> str:
    """
    decimal odds -> 英式分數盤 a:b
    decimal = fractional + 1
    """
    try:
        if decimal_odds is None:
            return "—"
        if float(decimal_odds) <= 1:
            return "0:1"
        frac = float(decimal_odds) - 1.0
        f = Fraction(frac).limit_denominator(max_denominator)
        return f"{f.numerator}:{f.denominator}"
    except Exception:
        return "—"


def predict_match(match: Dict) -> Dict:
    base = odds_to_probs(match["odds"])
    pick = max(base, key=base.get)
    seed = _seed_from(match["id"])
    bonus = 0.02 + (seed % 6) * 0.01
    result = dict(base)
    losers = [k for k in ("home", "draw", "away") if k != pick]
    result[pick] = min(0.78, result[pick] + bonus)
    deduction = result[pick] - base[pick]
    result[losers[0]] = max(0.08, result[losers[0]] - deduction * 0.55)
    result[losers[1]] = max(0.08, result[losers[1]] - deduction * 0.45)
    total = result["home"] + result["draw"] + result["away"]
    result = {k: v / total for k, v in result.items()}

    labels = {"home": "主勝", "draw": "和局", "away": "客勝"}
    sport_key = match["sport"] if match["sport"] in FACTOR_TEMPLATES else "足球"
    template = FACTOR_TEMPLATES[sport_key]
    weights = []
    for i, label in enumerate(template):
        w = 8 + ((seed + i * 17) % 14)
        weights.append({"label": label, "color": FACTOR_COLORS[i % len(FACTOR_COLORS)], "value": w})
    factor_total = sum(x["value"] for x in weights)
    factors = [
        {
            "label": x["label"],
            "color": x["color"],
            "value": round((x["value"] / factor_total) * 100),
        }
        for x in weights
    ]
    fix = 100 - sum(x["value"] for x in factors)
    factors[0]["value"] += fix
    top = max(factors, key=lambda x: x["value"])

    side = match["home"] if pick == "home" else match["away"] if pick == "away" else "和局"
    reason_title = "和局依據構成" if pick == "draw" else f"{side} 勝利依據構成"
    pick_odds = match["odds"][pick]

    conf_home = round(result["home"] * 100, 1)
    conf_draw = round(result["draw"] * 100, 1)
    conf_away = round(result["away"] * 100, 1)

    h2h_bets = [
        {
            "outcomeKey": "home",
            "label": "主勝",
            "team": match["home"],
            "oddsDecimal": round(match["odds"]["home"], 2),
            "oddsFractional": decimal_to_fractional_odds(match["odds"]["home"]),
            "confidence": conf_home,
        },
        {
            "outcomeKey": "draw",
            "label": "和局",
            "team": "和局",
            "oddsDecimal": round(match["odds"]["draw"], 2),
            "oddsFractional": decimal_to_fractional_odds(match["odds"]["draw"]),
            "confidence": conf_draw,
        },
        {
            "outcomeKey": "away",
            "label": "客勝",
            "team": match["away"],
            "oddsDecimal": round(match["odds"]["away"], 2),
            "oddsFractional": decimal_to_fractional_odds(match["odds"]["away"]),
            "confidence": conf_away,
        },
    ]

    # 讓分：只顯示信心較高的一邊
    spreads = match.get("spreads") or {}
    spreads_bet = None
    if spreads and spreads.get("home") and spreads.get("away"):
        home = float(spreads["home"])
        away = float(spreads["away"])
        raw_home = 1 / home
        raw_away = 1 / away
        total = raw_home + raw_away
        p_home = raw_home / total
        p_away = raw_away / total

        if p_home >= p_away:
            spreads_bet = {
                "outcomeKey": "spreads_home",
                "label": f"讓分 {spreads.get('homePoint', '')}（主隊）".strip(),
                "team": match["home"],
                "oddsDecimal": round(home, 2),
                "oddsFractional": decimal_to_fractional_odds(home),
                "confidence": round(p_home * 100, 1),
            }
        else:
            spreads_bet = {
                "outcomeKey": "spreads_away",
                "label": f"讓分 {spreads.get('awayPoint', '')}（客隊）".strip(),
                "team": match["away"],
                "oddsDecimal": round(away, 2),
                "oddsFractional": decimal_to_fractional_odds(away),
                "confidence": round(p_away * 100, 1),
            }

    # 總分：只顯示信心較高的 Over/Under
    totals = match.get("totals") or {}
    totals_bet = None
    if totals and totals.get("over") and totals.get("under") and totals.get("point") is not None:
        over = float(totals["over"])
        under = float(totals["under"])
        p_over = (1 / over) / ((1 / over) + (1 / under))
        p_under = (1 / under) / ((1 / over) + (1 / under))

        point = totals.get("point")
        if p_over >= p_under:
            totals_bet = {
                "outcomeKey": "totals_over",
                "label": f"總分 大 {point}",
                "team": f"大 {point}",
                "oddsDecimal": round(over, 2),
                "oddsFractional": decimal_to_fractional_odds(over),
                "confidence": round(p_over * 100, 1),
            }
        else:
            totals_bet = {
                "outcomeKey": "totals_under",
                "label": f"總分 小 {point}",
                "team": f"小 {point}",
                "oddsDecimal": round(under, 2),
                "oddsFractional": decimal_to_fractional_odds(under),
                "confidence": round(p_under * 100, 1),
            }

    # 排序：h2h 依信心高到低，再接上讓分/大小分（如果有）
    h2h_bets_sorted = sorted(h2h_bets, key=lambda x: x.get("confidence", 0), reverse=True)
    ordered_bets = h2h_bets_sorted
    if spreads_bet:
        ordered_bets.append(spreads_bet)
    if totals_bet:
        ordered_bets.append(totals_bet)

    return {
        "pick": pick,
        "pickLabel": labels[pick],
        "market": match.get("market") or "獨贏",
        "recommendedTeam": side,
        "recommendedOdds": round(pick_odds, 2),
        "confidence": round(result[pick] * 100, 1),
        "bets": ordered_bets,
        "outcomeSegments": [
            {"label": "主勝", "value": round(result["home"] * 100), "color": "#fb7185"},
            {"label": "和局", "value": round(result["draw"] * 100), "color": "#34d399"},
            {"label": "客勝", "value": round(result["away"] * 100), "color": "#60a5fa"},
        ],
        "reasonTitle": reason_title,
        "reasonSegments": factors,
        "topFactor": top,
        "title": f"{match['market']} · {side}",
        "subtitle": f"{match['league']} · {match['home']} vs {match['away']} · 賠率 {round(pick_odds, 2)}",
    }


def get_data_source_label(matches: List[Dict]) -> str:
    if not matches:
        remaining = ODDS_API_META.get("requests_remaining")
        if remaining is not None:
            return f"The Odds API 尚無快取，本月剩餘 {remaining} 次"
        if not get_odds_api_key():
            return "請設定 ODDS_API_KEY 後由管理員更新賽事"
        return "尚無賽事，請管理員按「更新賽事資料」"
    if any(m.get("source") == "the-odds-api" for m in matches):
        remaining = ODDS_API_META.get("requests_remaining")
        quota = f"（本月剩餘 {remaining} 次）" if remaining is not None else ""
        return f"The Odds API 國際莊家真實賠率{quota}"
    return "本地快取資料"


def get_odds_api_meta() -> dict:
    return dict(ODDS_API_META)
