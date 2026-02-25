#!/usr/bin/env python3
"""
Portfolio Fundamental News Pipeline
------------------------------------
Modes:
  --mode daily   : Aggressive 24h thesis-signal scan. Surfaces only thesis_breaking
                   and thesis_confirming items. Writes alerts.json for urgent review.
  --mode weekly  : Broad 7-day sweep. Collects all fundamental news, updates
                   latest.json, archives a weekly snapshot.

New in this version:
  - thesis_signal field on every news item: thesis_breaking | thesis_confirming | noise
  - Separate daily alert prompt focused on "has the story changed?"
  - Weekly snapshots: site/data/archive/week_YYYY-MM-DD.json
  - last_earnings_outcome in earnings_timeline (beat | miss | in_line | unknown)
  - tickers.json supports optional per-ticker thesis notes (used when available)
"""
import argparse
import json
import os
import re
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
CURRENT_NEWS_DAYS = 14
ARCHIVE_RETENTION_DAYS = 365
DEFAULT_MODEL_CANDIDATES = ["gpt-4o-search-preview", "gpt-4.1-mini"]

THEME_RULES = {
    "guidance": {
        "label": "Guidance / Outlook",
        "priority": "high",
        "keywords": ["guidance", "outlook", "forecast", "raises guidance", "cuts guidance"],
    },
    "earnings": {
        "label": "Earnings Quality",
        "priority": "high",
        "keywords": ["earnings", "eps", "revenue", "beat", "miss", "margin", "quarterly results"],
    },
    "legal_regulatory": {
        "label": "Legal / Regulatory",
        "priority": "high",
        "keywords": ["sec", "investigation", "lawsuit", "regulator", "fine", "antitrust", "settlement"],
    },
    "merger_acquisition": {
        "label": "M&A / Strategic Deal",
        "priority": "high",
        "keywords": ["acquisition", "acquire", "merger", "takeover", "deal", "divestiture", "spin-off"],
    },
    "management_governance": {
        "label": "Management / Governance",
        "priority": "medium",
        "keywords": ["ceo", "cfo", "resigns", "board", "chairman", "governance", "executive"],
    },
    "capital_structure": {
        "label": "Balance Sheet / Financing",
        "priority": "medium",
        "keywords": ["debt", "refinancing", "bond", "offering", "liquidity", "cash flow", "bankruptcy"],
    },
    "shareholder_returns": {
        "label": "Capital Returns",
        "priority": "medium",
        "keywords": ["buyback", "repurchase", "dividend", "special dividend"],
    },
    "product_strategy": {
        "label": "Product / Strategy",
        "priority": "medium",
        "keywords": ["approval", "launch", "contract", "partnership", "pipeline", "roadmap"],
    },
}

TEMPORARY_NOISE_KEYWORDS = [
    "daily", "intraday", "today", "technical", "chart", "rally", "dip",
    "overbought", "oversold", "short squeeze", "options activity", "price target", "rumor",
]

DURABLE_BOOST_KEYWORDS = [
    "multi-year", "long-term", "strategic review", "restructuring",
    "cost transformation", "capital allocation", "five-year", "three-year",
]

# Keywords that strongly suggest a thesis-level change regardless of theme
THESIS_BREAKING_SIGNALS = [
    "bankrupt", "fraud", "sec charges", "delisted", "class action",
    "loses license", "banned", "shutdown", "catastrophic", "existential",
    "business model", "structural decline", "disrupted", "obsolete",
    "market share collapse", "lost contract", "cancelled", "regulatory block",
    "forced divestiture", "nationalized", "major breach", "data breach",
    "ceo arrested", "accounting irregularity", "restatement",
]

THESIS_CONFIRMING_SIGNALS = [
    "market share gain", "record revenue", "record earnings", "record profit",
    "new market", "major contract", "strategic win", "dominant", "monopoly",
    "raised guidance", "beats estimates", "exceeded expectations",
    "breakthrough", "patent granted", "regulatory approval", "landmark deal",
]


# ── Utilities ──────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    temp.replace(path)


def load_tickers(path: Path) -> List[Dict[str, Any]]:
    """
    Supports two formats:
      Simple:   { "tickers": ["AAPL", "MSFT"] }
      Extended: { "holdings": [{"ticker": "AAPL", "thesis": "..."}, ...] }
    Always returns a list of dicts with at least {"ticker": "AAPL", "thesis": null}.
    """
    payload = read_json(path, {})

    # Extended format
    if "holdings" in payload:
        holdings = payload["holdings"]
        if not isinstance(holdings, list) or not holdings:
            raise ValueError("tickers.json 'holdings' must be a non-empty list")
        result = []
        for h in holdings:
            if isinstance(h, str):
                result.append({"ticker": h.strip().upper(), "thesis": None})
            elif isinstance(h, dict):
                t = str(h.get("ticker", "")).strip().upper()
                if t:
                    result.append({"ticker": t, "thesis": h.get("thesis") or None})
        return result

    # Simple format (backwards compatible)
    tickers = payload.get("tickers", [])
    if not isinstance(tickers, list) or not tickers:
        raise ValueError("tickers.json must contain a non-empty 'tickers' list")
    return [{"ticker": str(t).strip().upper(), "thesis": None} for t in tickers if str(t).strip()]


def get_ssl_context() -> ssl.SSLContext:
    if os.getenv("OPENAI_TLS_INSECURE", "").strip().lower() in {"1", "true", "yes"}:
        return ssl._create_unverified_context()  # noqa: SLF001
    custom_bundle = os.getenv("OPENAI_CA_BUNDLE", "").strip()
    if custom_bundle:
        return ssl.create_default_context(cafile=custom_bundle)
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def post_json(url: str, payload: Dict[str, Any], api_key: str) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url, data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "stocks-news-updater/1.0",
        },
        method="POST",
    )
    with urlopen(req, timeout=60, context=get_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def responses_with_fallback(payload: Dict[str, Any], api_key: str, preferred_model: str) -> Dict[str, Any]:
    candidates = [preferred_model] + [m for m in DEFAULT_MODEL_CANDIDATES if m != preferred_model]
    errors: List[str] = []
    for model in candidates:
        try:
            candidate_payload = dict(payload)
            candidate_payload["model"] = model
            resp = post_json(OPENAI_RESPONSES_URL, candidate_payload, api_key)
            resp["_model_used"] = model
            return resp
        except Exception as exc:
            text = str(exc)
            errors.append(f"{model}: {text}")
            if "404" not in text and "model" not in text.lower():
                break
    raise RuntimeError("OpenAI Responses request failed. Tried: " + " | ".join(errors))


def extract_output_text(resp: Dict[str, Any]) -> str:
    if isinstance(resp.get("output_text"), str) and resp["output_text"].strip():
        return resp["output_text"]
    chunks: List[str] = []
    for out in resp.get("output", []):
        for content in out.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks).strip()


def parse_json_from_text(text: str) -> Any:
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    candidate = fenced.group(1).strip() if fenced else text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start != -1 and end != -1 and end > start:
            return json.loads(candidate[start: end + 1])
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(candidate[start: end + 1])
        raise


def parse_json_object_from_text(text: str) -> Dict[str, Any]:
    parsed = parse_json_from_text(text)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return parsed[0]
    return {}


def pick_value(obj: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for key in keys:
        value = obj.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return default


# ── Date helpers ───────────────────────────────────────────────────────────────

def parse_date_value(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def extract_dates_from_text(text: str) -> List[str]:
    if not text:
        return []
    found: List[str] = []
    for y, m, d in re.findall(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
        found.append(f"{y}-{m}-{d}")
    month_re = (
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
        r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+(\d{1,2}),\s*(20\d{2})\b"
    )
    for mon, day, year in re.findall(month_re, text, flags=re.I):
        try:
            dt = datetime.strptime(f"{mon} {day} {year}", "%b %d %Y")
        except ValueError:
            try:
                dt = datetime.strptime(f"{mon} {day} {year}", "%B %d %Y")
            except ValueError:
                continue
        found.append(dt.strftime("%Y-%m-%d"))
    out: List[str] = []
    seen = set()
    for d in found:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def parse_datetime_to_iso(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = str(value).strip()
    candidates = [raw, raw.replace("Z", "+00:00")] if raw.endswith("Z") else [raw]
    for c in candidates:
        try:
            return iso(datetime.fromisoformat(c))
        except ValueError:
            pass
    return None


# ── Classification ─────────────────────────────────────────────────────────────

def classify_fundamental_event(headline: str, summary: str = "") -> Optional[Dict[str, str]]:
    text = f"{headline} {summary}".lower()
    if any(noise in text for noise in TEMPORARY_NOISE_KEYWORDS):
        return None
    durable_boost = any(k in text for k in DURABLE_BOOST_KEYWORDS)
    for theme_key, meta in THEME_RULES.items():
        for keyword in meta["keywords"]:
            if keyword in text:
                return {
                    "theme": theme_key,
                    "theme_label": meta["label"],
                    "priority": "high" if durable_boost else meta["priority"],
                    "horizon_fit": "durable" if durable_boost else "possible",
                    "matched_keyword": keyword,
                }
    return None


def classify_thesis_signal_heuristic(headline: str, summary: str, why_it_matters: str = "") -> str:
    """
    Local keyword heuristic — used as fallback when GPT doesn't supply thesis_signal.
    Returns: thesis_breaking | thesis_confirming | noise
    """
    text = f"{headline} {summary} {why_it_matters}".lower()
    if any(k in text for k in THESIS_BREAKING_SIGNALS):
        return "thesis_breaking"
    if any(k in text for k in THESIS_CONFIRMING_SIGNALS):
        return "thesis_confirming"
    return "noise"


def normalize_news_item(item: Dict[str, Any], ticker: str) -> Optional[Dict[str, Any]]:
    headline = (item.get("headline") or "").strip()
    summary = (item.get("summary") or "").strip()
    if not headline:
        return None

    event = classify_fundamental_event(headline, summary)
    if not event:
        return None

    dt_iso = parse_datetime_to_iso(item.get("published_at"))
    if not dt_iso:
        return None

    url = (item.get("url") or "").strip()
    source = (item.get("source") or "").strip() or "Unknown"
    why = (item.get("why_it_matters") or "").strip()

    # Prefer GPT-supplied thesis_signal; fall back to heuristic
    gpt_signal = (item.get("thesis_signal") or "").strip().lower()
    if gpt_signal not in {"thesis_breaking", "thesis_confirming", "noise"}:
        gpt_signal = classify_thesis_signal_heuristic(headline, summary, why)

    return {
        "id": f"{ticker}:{url or headline[:80]}",
        "ticker": ticker,
        "headline": headline,
        "summary": summary,
        "source": source,
        "url": url,
        "datetime": dt_iso,
        "theme": event["theme"],
        "theme_label": event["theme_label"],
        "priority": event["priority"],
        "horizon_fit": event["horizon_fit"],
        "matched_keyword": event["matched_keyword"],
        "thesis_signal": gpt_signal,
        "why_it_matters": why,
    }


# ── Earnings timeline ──────────────────────────────────────────────────────────

def normalize_earnings_timeline(timeline: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    last_raw = timeline.get("last_earnings_report_date")
    next_raw = timeline.get("next_earnings_date")
    status = (timeline.get("next_earnings_status") or "unknown").strip().lower()
    notes = (timeline.get("notes") or "").strip()

    last_dt = parse_date_value(last_raw)
    next_dt = parse_date_value(next_raw)
    if next_dt and next_dt.date() < now.date():
        next_dt = None
        status = "unknown"

    warning = None
    note_dates = extract_dates_from_text(notes)
    future_note_dates = [d for d in note_dates if parse_date_value(d) and parse_date_value(d).date() >= now.date()]
    future_note_unique = list(dict.fromkeys(future_note_dates))

    if next_dt and last_dt and next_dt <= last_dt:
        next_dt = None
        status = "conflict"
        warning = "conflict_detected: next earnings date is not after last report date"
    elif len(future_note_unique) > 1:
        next_dt = None
        status = "conflict"
        warning = "conflict_detected: multiple future earnings dates found in sources"

    raw_outcome = (timeline.get("last_earnings_outcome") or "unknown").strip().lower()
    outcome = raw_outcome if raw_outcome in {"beat", "miss", "in_line", "unknown"} else "unknown"

    return {
        "last_earnings_report_date": last_dt.strftime("%Y-%m-%d") if last_dt else None,
        "last_earnings_label": timeline.get("last_earnings_label"),
        "last_earnings_outcome": outcome,
        "next_earnings_date": next_dt.strftime("%Y-%m-%d") if next_dt else None,
        "next_earnings_status": status if status in {"scheduled", "estimated", "unknown", "conflict"} else "unknown",
        "source_url": timeline.get("source_url"),
        "notes": warning or notes or None,
    }


def fetch_earnings_timeline_with_gpt(
    ticker: str, now: datetime, api_key: str, model: str,
) -> Dict[str, Any]:
    system_prompt = (
        "You are a financial research assistant. Search the web and return earnings timeline dates for the ticker. "
        "Use company investor relations, exchange, or trusted finance calendars when possible. "
        "Respond with exactly one JSON object and no markdown."
    )
    user_prompt = (
        f"Ticker: {ticker}\n"
        f"Today (UTC): {now.date().isoformat()}\n"
        "Return exactly this JSON object schema:\n"
        "- last_earnings_report_date (YYYY-MM-DD or null)\n"
        "- last_earnings_label (e.g., Q4 2025 or FY2025 Q4, else null)\n"
        "- last_earnings_outcome (one of: beat, miss, in_line, unknown — "
        "did the most recent quarter beat, miss, or meet analyst EPS/revenue estimates?)\n"
        "- next_earnings_date (YYYY-MM-DD or null)\n"
        "- next_earnings_status (scheduled|estimated|unknown)\n"
        "- source_url (best source used, else null)\n"
        "- notes (short text)\n"
        "Prefer the newest source publication or IR page."
    )
    payload = {
        "tools": [{"type": "web_search_preview"}],
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "max_output_tokens": 900,
    }
    try:
        resp = responses_with_fallback(payload, api_key, model)
        text = extract_output_text(resp)
        obj = parse_json_object_from_text(text)
    except Exception:
        obj = {}

    raw = {
        "last_earnings_report_date": pick_value(obj, ["last_earnings_report_date", "last_report_date", "last_earnings_date"], None),
        "last_earnings_label": pick_value(obj, ["last_earnings_label", "last_quarter_label"], None),
        "last_earnings_outcome": pick_value(obj, ["last_earnings_outcome", "earnings_outcome", "outcome"], "unknown"),
        "next_earnings_date": pick_value(obj, ["next_earnings_date", "next_report_date", "upcoming_earnings_date"], None),
        "next_earnings_status": pick_value(obj, ["next_earnings_status", "next_report_status"], "unknown"),
        "source_url": pick_value(obj, ["source_url", "earnings_source_url", "url"], None),
        "notes": pick_value(obj, ["notes", "note"], None),
    }
    return normalize_earnings_timeline(raw, now)


# ── News fetching ──────────────────────────────────────────────────────────────

def fetch_ticker_news_with_gpt(
    ticker: str,
    mode: str,
    now: datetime,
    api_key: str,
    model: str,
    thesis: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Weekly mode: broad 7-day sweep of all fundamental news, with thesis_signal on each item.
    Daily mode:  aggressive 24h scan — only surfaces thesis_breaking / thesis_confirming.
                 Returns [] if nothing material happened.
    """
    days = 1 if mode == "daily" else 7
    from_date = (now - timedelta(days=days)).date().isoformat()
    to_date = now.date().isoformat()

    thesis_context = (
        f"Known investment thesis for {ticker}: {thesis}\n"
        if thesis
        else (
            f"Use your general knowledge of why long-term investors typically hold {ticker} "
            "(its core business model, competitive moat, and key growth drivers) as the implied thesis.\n"
        )
    )

    if mode == "daily":
        system_prompt = (
            "You are a senior equity analyst watching for thesis-level changes for long-term investors. "
            "You only flag news that could force a 2-year holder to reconsider their position — "
            "NOT earnings beats/misses, analyst upgrades, routine updates, or daily price moves. "
            "Think: regulatory bans, business model disruption, loss of a key market, fraud, "
            "major strategic reversal, existential competitive threat, or a landmark win that "
            "dramatically and durably expands the company's moat. "
            "If nothing material happened in the last 24 hours, return an empty JSON array []. "
            "Respond with JSON only — no prose, no markdown."
        )
        user_prompt = (
            f"Ticker: {ticker}\n"
            f"Date window: {from_date} to {to_date} (last 24 hours)\n"
            f"{thesis_context}"
            "Return a JSON array. Each item must have: "
            "headline, summary, source, url, published_at (ISO), why_it_matters, "
            "thesis_signal (thesis_breaking | thesis_confirming).\n"
            "Only include items that would make a long-term holder take action. "
            "If nothing qualifies, return []."
        )
        max_tokens = 1400
    else:
        system_prompt = (
            "You are a market research analyst. Search the web and return company-news events that could "
            "affect a 2-year investment thesis. Exclude short-term price chatter, analyst price targets, "
            "and technical commentary. "
            "For each item, evaluate thesis_signal: thesis_breaking (story has fundamentally changed for worse), "
            "thesis_confirming (story is strengthening), or noise (relevant but doesn't change the thesis). "
            "Respond with JSON only."
        )
        user_prompt = (
            f"Ticker: {ticker}\n"
            f"Date window: {from_date} to {to_date}\n"
            f"{thesis_context}"
            "Return a JSON array. Each item must include: "
            "headline, summary, source, url, published_at (ISO if available), why_it_matters, "
            "thesis_signal (thesis_breaking | thesis_confirming | noise).\n"
            "Rules: newest credible sources first, include publish dates, "
            "exclude anything outside the date window, prefer primary-source reporting. "
            "Max 12 items."
        )
        max_tokens = 1800

    payload = {
        "tools": [{"type": "web_search_preview"}],
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "max_output_tokens": max_tokens,
    }

    resp = responses_with_fallback(payload, api_key, model)
    text = extract_output_text(resp)
    raw = parse_json_from_text(text)
    if isinstance(raw, dict):
        raw = raw.get("items", [])
    if not isinstance(raw, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for row in raw:
        if isinstance(row, dict):
            item = normalize_news_item(row, ticker)
            if item:
                normalized.append(item)

    normalized.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    return normalized[:10]


# ── News archiving ─────────────────────────────────────────────────────────────

def merge_and_archive_news(
    current: List[Dict[str, Any]],
    archive: List[Dict[str, Any]],
    new_items: List[Dict[str, Any]],
    now: datetime,
):
    by_id = {item["id"]: item for item in current}
    for item in new_items:
        by_id[item["id"]] = item

    merged_current = list(by_id.values())
    cutoff_current = now - timedelta(days=CURRENT_NEWS_DAYS)
    cutoff_archive = now - timedelta(days=ARCHIVE_RETENTION_DAYS)

    still_current = []
    moved_to_archive = []

    for item in merged_current:
        dt_str = item.get("datetime")
        if not dt_str:
            still_current.append(item)
            continue
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt < cutoff_current:
            moved_to_archive.append(item)
        else:
            still_current.append(item)

    archive_by_id = {item["id"]: item for item in archive}
    for item in moved_to_archive:
        archive_by_id[item["id"]] = item

    pruned_archive = [
        item for item in archive_by_id.values()
        if (lambda dt_str: dt_str is None or
            datetime.fromisoformat(dt_str.replace("Z", "+00:00")) >= cutoff_archive
            )(item.get("datetime"))
    ]

    still_current.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    pruned_archive.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    return still_current, pruned_archive


def save_weekly_snapshot(holdings: List[Dict[str, Any]], now: datetime, archive_dir: Path) -> None:
    """Save a complete frozen snapshot of this week's dashboard state."""
    week_start = (now - timedelta(days=now.weekday())).date().isoformat()
    snapshot_path = archive_dir / f"week_{week_start}.json"
    snapshot = {
        "snapshot_date": iso(now),
        "week_starting": week_start,
        "holdings": holdings,
    }
    write_json(snapshot_path, snapshot)
    print(f"  Weekly snapshot saved → {snapshot_path}")


# ── Main pipeline ──────────────────────────────────────────────────────────────

def run(mode: str, tickers_file: Path, site_data_dir: Path):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable")

    model = os.getenv("OPENAI_MODEL", "gpt-4o-search-preview")
    ticker_configs = load_tickers(tickers_file)
    now = utc_now()

    current_path = site_data_dir / "current_news.json"
    archive_path = site_data_dir / "archive" / "news_archive.json"
    latest_path = site_data_dir / "latest.json"
    alerts_path = site_data_dir / "alerts.json"

    current_news = read_json(current_path, [])
    archive_news = read_json(archive_path, [])

    all_new_items: List[Dict[str, Any]] = []
    holdings = []
    alert_items: List[Dict[str, Any]] = []

    for cfg in ticker_configs:
        ticker = cfg["ticker"]
        thesis = cfg.get("thesis")
        print(f"  Fetching {ticker} ({mode})…")

        try:
            ticker_news = fetch_ticker_news_with_gpt(ticker, mode, now, api_key, model, thesis=thesis)
            earnings_timeline = fetch_earnings_timeline_with_gpt(ticker, now, api_key, model)
        except RuntimeError as exc:
            msg = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in msg:
                raise RuntimeError(
                    "TLS certificate verification failed.\n"
                    "  pip install --upgrade certifi\n"
                    "  Or set OPENAI_CA_BUNDLE to your org CA PEM.\n"
                    "  Temp bypass: OPENAI_TLS_INSECURE=1"
                ) from exc
            raise

        all_new_items.extend(ticker_news)

        # Collect thesis-level items for alerts.json
        for item in ticker_news:
            if item.get("thesis_signal") in {"thesis_breaking", "thesis_confirming"}:
                alert_items.append(item)

        theme_counts: Dict[str, int] = {}
        for n in ticker_news:
            theme_counts[n["theme_label"]] = theme_counts.get(n["theme_label"], 0) + 1

        holdings.append({
            "ticker": ticker,
            "thesis": thesis,
            "fundamental_news": ticker_news,
            "fundamental_news_count": len(ticker_news),
            "durable_news_count": len([n for n in ticker_news if n.get("horizon_fit") == "durable"]),
            "thesis_breaking_count": len([n for n in ticker_news if n.get("thesis_signal") == "thesis_breaking"]),
            "thesis_confirming_count": len([n for n in ticker_news if n.get("thesis_signal") == "thesis_confirming"]),
            "themes": theme_counts,
            "earnings_timeline": earnings_timeline,
        })

        time.sleep(1.1)

    current_news, archive_news = merge_and_archive_news(current_news, archive_news, all_new_items, now)

    # Sort: thesis_breaking tickers first, then by news volume
    holdings = sorted(
        holdings,
        key=lambda x: (-x["thesis_breaking_count"], -x["fundamental_news_count"]),
    )

    tickers_with_changes = [h for h in holdings if h["fundamental_news_count"] > 0]
    durable_item_count = len([n for n in all_new_items if n.get("horizon_fit") == "durable"])
    breaking_count = len([n for n in all_new_items if n.get("thesis_signal") == "thesis_breaking"])

    latest = {
        "generated_at": iso(now),
        "mode": mode,
        "model": model,
        "tickers": [c["ticker"] for c in ticker_configs],
        "summary": {
            "fundamental_item_count": len(all_new_items),
            "durable_item_count": durable_item_count,
            "thesis_breaking_count": breaking_count,
            "tickers_with_changes": len(tickers_with_changes),
            "tickers_without_changes": len(holdings) - len(tickers_with_changes),
        },
        "holdings": holdings,
        "news_counts": {
            "current": len(current_news),
            "archive": len(archive_news),
        },
    }

    write_json(current_path, current_news)
    write_json(archive_path, archive_news)
    write_json(latest_path, latest)

    # Merge new alert items into rolling 30-day alerts.json
    existing_alerts = read_json(alerts_path, [])
    alert_by_id = {a["id"]: a for a in existing_alerts}
    for item in alert_items:
        alert_by_id[item["id"]] = {**item, "alerted_at": iso(now), "mode": mode}
    cutoff_alerts = now - timedelta(days=30)
    fresh_alerts = [
        a for a in alert_by_id.values()
        if (dt := a.get("alerted_at")) and datetime.fromisoformat(dt) >= cutoff_alerts
    ]
    fresh_alerts.sort(key=lambda x: x.get("alerted_at") or "", reverse=True)
    write_json(alerts_path, fresh_alerts)

    # Weekly snapshot
    if mode == "weekly":
        save_weekly_snapshot(holdings, now, site_data_dir / "archive")

    breaking_tickers = [h["ticker"] for h in holdings if h["thesis_breaking_count"] > 0]
    print(
        f"\n✓ Done ({mode}): {len(all_new_items)} items · "
        f"{breaking_count} thesis-breaking"
        + (f" [{', '.join(breaking_tickers)}]" if breaking_tickers else "")
    )


def main():
    parser = argparse.ArgumentParser(description="Update stocks dashboard data")
    parser.add_argument("--mode", choices=["daily", "weekly"], required=True)
    parser.add_argument("--tickers-file", default="config/tickers.json", type=Path)
    parser.add_argument("--site-data-dir", default="site/data", type=Path)
    args = parser.parse_args()
    run(args.mode, args.tickers_file, args.site_data_dir)


if __name__ == "__main__":
    main()
