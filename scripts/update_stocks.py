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
import csv
import io
import json
import os
import re
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
ALPHA_VANTAGE_API_URL = "https://www.alphavantage.co/query"
CURRENT_NEWS_DAYS = 14
ARCHIVE_RETENTION_DAYS = 365
PRIMARY_MODEL = "gpt-4.1-mini"
FALLBACK_MODEL = "gpt-4.1-mini"

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
    "daily", "intraday", "technical", "overbought", "oversold",
    "short squeeze", "options activity", "price target", "rumor",
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

TRUSTED_EARNINGS_SOURCE_HINTS = [
    "investor",
    "ir.",
    "sec.gov",
    "nasdaq.com",
    "nyse.com",
    "bloomberg.com",
    "reuters.com",
    "marketwatch.com",
    "finance.yahoo.com",
    "companiesmarketcap.com",
    "earningswhispers.com",
]

SEC_COMPANY_TICKERS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


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


def get_json(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    req = Request(url, headers=headers or {}, method="GET")
    with urlopen(req, timeout=60, context=get_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_text(url: str, headers: Optional[Dict[str, str]] = None) -> str:
    req = Request(url, headers=headers or {}, method="GET")
    with urlopen(req, timeout=60, context=get_ssl_context()) as resp:
        return resp.read().decode("utf-8", errors="replace")


def responses_with_fallback(payload: Dict[str, Any], api_key: str, preferred_model: str) -> Dict[str, Any]:
    errors: List[str] = []
    retry_attempts = max(1, int(os.getenv("OPENAI_RETRY_ATTEMPTS", "3")))
    retry_base_seconds = max(0.5, float(os.getenv("OPENAI_RETRY_BASE_SECONDS", "1.5")))
    models_to_try = [preferred_model]
    if preferred_model != FALLBACK_MODEL:
        models_to_try.append(FALLBACK_MODEL)
    for model in models_to_try:
        for attempt in range(retry_attempts):
            try:
                candidate_payload = dict(payload)
                candidate_payload["model"] = model
                resp = post_json(OPENAI_RESPONSES_URL, candidate_payload, api_key)
                resp["_model_used"] = model
                return resp
            except HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace").strip()
                except Exception:
                    body = ""
                detail = body or str(exc)
                errors.append(f"{model}: HTTP {exc.code}: {detail}")
                if exc.code in {429, 500, 502, 503, 504} and attempt < retry_attempts - 1:
                    time.sleep(retry_base_seconds * (2 ** attempt))
                    continue
                break
            except Exception as exc:
                text = str(exc)
                errors.append(f"{model}: {text}")
                if "timed out" in text.lower() and attempt < retry_attempts - 1:
                    time.sleep(retry_base_seconds * (2 ** attempt))
                    continue
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
        # Model output can include prose plus multiple JSON snippets.
        # Scan for the first decodable JSON object/array block.
        decoder = json.JSONDecoder()
        for i, ch in enumerate(candidate):
            if ch not in "[{":
                continue
            try:
                value, _end = decoder.raw_decode(candidate, i)
                if isinstance(value, (dict, list)):
                    return value
            except json.JSONDecodeError:
                continue
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
            dt = datetime.fromisoformat(c)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return iso(dt)
        except ValueError:
            pass
    # Last resort: plain date string e.g. "2026-02-24"
    try:
        dt = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return iso(dt)
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

    # Prefer GPT-supplied thesis_signal; accept both short and long forms
    _signal_aliases = {
        "breaking": "thesis_breaking",
        "confirming": "thesis_confirming",
        "thesis_breaking": "thesis_breaking",
        "thesis_confirming": "thesis_confirming",
        "noise": "noise",
    }
    gpt_signal = _signal_aliases.get((item.get("thesis_signal") or "").strip().lower(), "")
    if not gpt_signal:
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
    outcome_text = f"{raw_outcome} {notes}".lower()
    if raw_outcome in {"beat", "miss", "in_line", "unknown"}:
        outcome = raw_outcome
    else:
        beat_hit = any(k in outcome_text for k in ["beat", "beats", "above expectations", "exceeded", "surpassed", "topped estimates"])
        miss_hit = any(k in outcome_text for k in ["miss", "missed", "below expectations", "fell short", "disappoint"])
        inline_hit = any(k in outcome_text for k in ["in line", "inline", "in-line", "met expectations", "as expected"])
        if beat_hit and miss_hit:
            outcome = "in_line"
        elif beat_hit:
            outcome = "beat"
        elif miss_hit:
            outcome = "miss"
        elif inline_hit:
            outcome = "in_line"
        else:
            outcome = "unknown"

    source_url = (timeline.get("source_url") or "").strip()
    source_lc = source_url.lower()
    source_is_trusted = bool(source_url) and any(h in source_lc for h in TRUSTED_EARNINGS_SOURCE_HINTS)

    # Anti-hallucination guardrail: do not publish speculative next dates.
    # If the model cannot provide a trusted source-backed scheduled date, mark unknown.
    if next_dt and (status != "scheduled" or not source_is_trusted):
        next_dt = None
        status = "unknown"

    if status == "estimated":
        status = "unknown"
        next_dt = None

    return {
        "last_earnings_report_date": last_dt.strftime("%Y-%m-%d") if last_dt else None,
        "last_earnings_label": timeline.get("last_earnings_label"),
        "last_earnings_outcome": outcome,
        "next_earnings_date": next_dt.strftime("%Y-%m-%d") if next_dt else None,
        "next_earnings_status": status if status in {"scheduled", "estimated", "unknown", "conflict"} else "unknown",
        "source_url": source_url or None,
        "notes": warning or notes or None,
    }


def infer_earnings_outcome_from_text(text: str) -> str:
    t = (text or "").lower()
    beat_terms = [
        "beat", "beats", "above expectations", "exceeded", "surpassed",
        "topped estimates", "record revenue", "record earnings", "raises guidance",
    ]
    miss_terms = [
        "miss", "missed", "below expectations", "fell short",
        "guidance cut", "cut guidance", "disappoint",
    ]
    inline_terms = ["in line", "inline", "in-line", "met expectations", "as expected"]
    # Common strong-positive earnings phrasing even when "beat" is omitted.
    if re.search(r"\bup\s+\d+(\.\d+)?%\s+year over year\b", t):
        beat_terms.append("year-over-year-growth-signal")

    beat_hit = any(k in t for k in beat_terms)
    miss_hit = any(k in t for k in miss_terms)
    inline_hit = any(k in t for k in inline_terms)
    if beat_hit and miss_hit:
        return "in_line"
    if beat_hit:
        return "beat"
    if miss_hit:
        return "miss"
    if inline_hit:
        return "in_line"
    return "unknown"


def reconcile_earnings_timeline_with_news(
    earnings_timeline: Dict[str, Any], news_items: List[Dict[str, Any]], now: datetime
) -> Dict[str, Any]:
    def is_earnings_item(item: Dict[str, Any]) -> bool:
        if item.get("theme") == "earnings":
            return True
        txt = f"{item.get('headline','')} {item.get('summary','')}".lower()
        return bool(re.search(r"\b(earnings|eps|quarterly results|q[1-4])\b", txt))

    earnings_news = [n for n in news_items if is_earnings_item(n)]
    if not earnings_news:
        return earnings_timeline

    earnings_news.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    recent = earnings_news[0]
    recent_dt = None
    if recent.get("datetime"):
        try:
            recent_dt = datetime.fromisoformat(str(recent["datetime"]).replace("Z", "+00:00"))
        except Exception:
            recent_dt = None

    merged_text = " ".join(
        f"{n.get('headline','')} {n.get('summary','')} {n.get('why_it_matters','')}" for n in earnings_news[:3]
    )
    inferred_outcome = infer_earnings_outcome_from_text(merged_text)

    reconciled = dict(earnings_timeline)
    current_outcome = (reconciled.get("last_earnings_outcome") or "unknown").strip().lower()
    if inferred_outcome != "unknown":
        if current_outcome in {"unknown", "in_line"}:
            reconciled["last_earnings_outcome"] = inferred_outcome
        elif current_outcome == "miss" and inferred_outcome == "beat":
            reconciled["last_earnings_outcome"] = "beat"
        elif current_outcome == "beat" and inferred_outcome == "miss":
            reconciled["last_earnings_outcome"] = "in_line"

    # If there's a fresh earnings report headline in the last 10 days, use its date as last report fallback.
    if recent_dt and recent_dt >= now - timedelta(days=10):
        last_dt = parse_date_value(reconciled.get("last_earnings_report_date"))
        if last_dt is None or recent_dt.date() > last_dt.date():
            reconciled["last_earnings_report_date"] = recent_dt.strftime("%Y-%m-%d")
            if not reconciled.get("source_url"):
                reconciled["source_url"] = recent.get("url")

    note = (reconciled.get("notes") or "").strip()
    if inferred_outcome != "unknown" and "reconciled_from_news" not in note:
        reconciled["notes"] = ("; ".join([note, "reconciled_from_news"]).strip("; ")).strip() or "reconciled_from_news"

    return normalize_earnings_timeline(reconciled, now)


def sec_headers() -> Dict[str, str]:
    ua = os.getenv("SEC_USER_AGENT", "portfolio-news-bot/1.0 (contact: admin@example.com)")
    return {
        "User-Agent": ua,
        "Accept-Encoding": "gzip, deflate",
    }


def get_sec_ticker_map() -> Dict[str, Dict[str, Any]]:
    global SEC_COMPANY_TICKERS_CACHE
    if SEC_COMPANY_TICKERS_CACHE is not None:
        return SEC_COMPANY_TICKERS_CACHE
    raw = get_json(SEC_TICKERS_URL, headers=sec_headers())
    out: Dict[str, Dict[str, Any]] = {}
    for _, row in raw.items():
        if not isinstance(row, dict):
            continue
        t = str(row.get("ticker", "")).strip().upper()
        if not t:
            continue
        cik = int(row.get("cik_str", 0))
        out[t] = {"cik": f"{cik:010d}", "title": row.get("title")}
    SEC_COMPANY_TICKERS_CACHE = out
    return out


def fetch_sec_news_items(ticker: str, now: datetime) -> List[Dict[str, Any]]:
    ticker_map = get_sec_ticker_map()
    meta = ticker_map.get(ticker)
    if not meta:
        return []
    data = get_json(SEC_SUBMISSIONS_URL.format(cik=meta["cik"]), headers=sec_headers())
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form", []) or []
    filing_dates = recent.get("filingDate", []) or []
    accession_numbers = recent.get("accessionNumber", []) or []
    primary_docs = recent.get("primaryDocument", []) or []
    if not isinstance(forms, list):
        return []

    cutoff = (now - timedelta(days=7)).date()
    items: List[Dict[str, Any]] = []
    for i, form in enumerate(forms[:80]):
        f = str(form or "").strip().upper()
        if f not in {"8-K", "10-Q", "10-K", "6-K", "DEF 14A"}:
            continue
        date_raw = filing_dates[i] if i < len(filing_dates) else None
        dt = parse_date_value(date_raw)
        if not dt or dt.date() < cutoff:
            continue
        accession = str(accession_numbers[i] if i < len(accession_numbers) else "").strip()
        primary_doc = str(primary_docs[i] if i < len(primary_docs) else "").strip()
        cik_int = str(int(meta["cik"])) if str(meta["cik"]).isdigit() else str(meta["cik"])
        acc_no_dash = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{primary_doc}" if accession and primary_doc else ""
        headline = f"{ticker} SEC filing {f}"
        summary = {
            "8-K": "Material corporate update filed with SEC; may include earnings, strategy, governance, or legal changes.",
            "10-Q": "Quarterly report filed with SEC including revenue, margins, guidance context, and risk updates.",
            "10-K": "Annual report filed with SEC with business model, financials, and risk-factor updates.",
            "6-K": "Foreign issuer current report filed with SEC with potentially material operational updates.",
            "DEF 14A": "Proxy filing with governance and management-related disclosures.",
        }.get(f, "SEC filing with potentially material fundamentals.")
        items.append({
            "headline": headline,
            "summary": summary,
            "source": "SEC",
            "url": url,
            "published_at": dt.strftime("%Y-%m-%d"),
            "why_it_matters": "Primary-source regulatory filing.",
            "thesis_signal": "noise",
        })
    return items


def parse_gdelt_seendate(raw: Any) -> Optional[str]:
    s = str(raw or "").strip()
    if re.fullmatch(r"\d{14}", s):
        try:
            dt = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return iso(dt)
        except ValueError:
            return None
    return parse_datetime_to_iso(s)


def fetch_gdelt_news_items(ticker: str) -> List[Dict[str, Any]]:
    query = f'{ticker} (earnings OR guidance OR acquisition OR merger OR lawsuit OR SEC OR investigation OR contract OR partnership OR CEO)'
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": "50",
        "format": "json",
        "sort": "datedesc",
        "timespan": "7d",
    }
    url = f"{GDELT_DOC_API_URL}?{urlencode(params, quote_via=quote_plus)}"
    payload = get_json(url)
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        return []
    items: List[Dict[str, Any]] = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip()
        if not title:
            continue
        dt_iso = parse_gdelt_seendate(a.get("seendate"))
        if not dt_iso:
            continue
        source = str(a.get("domain") or "Unknown").strip()
        url_a = str(a.get("url") or "").strip()
        items.append({
            "headline": title,
            "summary": str(a.get("socialimage") or "").strip(),
            "source": source,
            "url": url_a,
            "published_at": dt_iso,
            "why_it_matters": "Cross-source media signal from GDELT.",
            "thesis_signal": "noise",
        })
    return items


def fetch_alpha_earnings_timeline(ticker: str, now: datetime) -> Dict[str, Any]:
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    if not api_key:
        return normalize_earnings_timeline({}, now)
    params = {
        "function": "EARNINGS_CALENDAR",
        "symbol": ticker,
        "horizon": "3month",
        "apikey": api_key,
    }
    url = f"{ALPHA_VANTAGE_API_URL}?{urlencode(params, quote_via=quote_plus)}"
    raw_csv = get_text(url)
    if not raw_csv.strip():
        return normalize_earnings_timeline({}, now)
    rows = list(csv.DictReader(io.StringIO(raw_csv)))
    today = now.date()
    next_date: Optional[str] = None
    for row in rows:
        if str(row.get("symbol", "")).strip().upper() != ticker:
            continue
        d = parse_date_value(row.get("reportDate"))
        if not d:
            continue
        if d.date() >= today:
            next_date = d.strftime("%Y-%m-%d")
            break
    return normalize_earnings_timeline({
        "next_earnings_date": next_date,
        "next_earnings_status": "estimated" if next_date else "unknown",
        "source_url": "https://www.alphavantage.co/documentation/#earnings-calendar",
        "notes": "source:alpha_vantage",
    }, now)


def merge_news_items_for_ticker(ticker: str, raw_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for row in raw_items:
        if isinstance(row, dict):
            item = normalize_news_item(row, ticker)
            if item:
                normalized.append(item)
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in normalized:
        by_id[item["id"]] = item
    merged = list(by_id.values())
    merged.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    return merged[:10]


def fetch_ticker_data_from_free_sources(
    ticker: str, now: datetime
) -> tuple:
    all_raw_news: List[Dict[str, Any]] = []
    notes: List[str] = []

    try:
        sec_news = fetch_sec_news_items(ticker, now)
        all_raw_news.extend(sec_news)
        notes.append(f"sec_news={len(sec_news)}")
    except Exception as exc:
        notes.append(f"sec_error={str(exc)[:120]}")

    try:
        gdelt_news = fetch_gdelt_news_items(ticker)
        all_raw_news.extend(gdelt_news)
        notes.append(f"gdelt_news={len(gdelt_news)}")
    except Exception as exc:
        notes.append(f"gdelt_error={str(exc)[:120]}")

    news_items = merge_news_items_for_ticker(ticker, all_raw_news)

    try:
        earnings_timeline = fetch_alpha_earnings_timeline(ticker, now)
        notes.append("alpha=ok" if os.getenv("ALPHA_VANTAGE_API_KEY", "").strip() else "alpha=missing_key")
    except Exception as exc:
        earnings_timeline = normalize_earnings_timeline({}, now)
        notes.append(f"alpha_error={str(exc)[:120]}")

    earnings_timeline = reconcile_earnings_timeline_with_news(earnings_timeline, news_items, now)
    note = (earnings_timeline.get("notes") or "").strip()
    extra = "; ".join(notes)
    earnings_timeline["notes"] = ("; ".join([note, extra]).strip("; ")).strip() or None

    print(f"    {ticker}: free-sources raw={len(all_raw_news)} normalized={len(news_items)}")
    return news_items, earnings_timeline


def fetch_ticker_data_with_gpt(
    ticker: str,
    now: datetime,
    api_key: str,
    model: str,
    thesis: Optional[str] = None,
) -> tuple:
    """
    Single API call per ticker — returns (news_items, earnings_timeline).
    Fetches the last 7 days of fundamental news AND earnings timeline in one shot.
    """
    from_date = (now - timedelta(days=7)).date().isoformat()
    to_date = now.date().isoformat()

    thesis_context = (
        f"Known investment thesis for {ticker}: {thesis}\n"
        if thesis
        else (
            f"Use your general knowledge of why long-term investors typically hold {ticker} "
            "(its core business model, competitive moat, and key growth drivers) as the implied thesis.\n"
        )
    )

    system_prompt = (
        "You are a market research analyst and financial data assistant. "
        "Search the web and return two things for the given ticker in a single JSON object:\n"
        "1. Fundamental news from the last 7 days that could affect a 2-year investment thesis.\n"
        "2. The earnings timeline (last and next earnings dates and outcome).\n"
        "Exclude short-term price moves, analyst price targets, and technical commentary.\n"
        "Respond with JSON only — no prose, no markdown."
    )

    user_prompt = (
        f"Ticker: {ticker}\n"
        f"Today (UTC): {now.date().isoformat()}\n"
        f"News window: {from_date} to {to_date}\n"
        f"{thesis_context}\n"
        "Return exactly this JSON structure:\n"
        "{\n"
        '  "news": [\n'
        "    {\n"
        '      "headline": "...",\n'
        '      "summary": "1-2 sentences",\n'
        '      "source": "publication name",\n'
        '      "url": "direct article url",\n'
        '      "published_at": "YYYY-MM-DD or ISO datetime",\n'
        '      "why_it_matters": "how this affects the long-term thesis",\n'
        '      "thesis_signal": "thesis_breaking | thesis_confirming | noise"\n'
        "    }\n"
        "  ],\n"
        '  "earnings": {\n'
        '    "last_earnings_report_date": "YYYY-MM-DD or null",\n'
        '    "last_earnings_label": "e.g. Q4 FY2025 or null",\n'
        '    "last_earnings_outcome": "beat | miss | in_line | unknown",\n'
        '    "next_earnings_date": "YYYY-MM-DD or null",\n'
        '    "next_earnings_status": "scheduled | estimated | unknown",\n'
        '    "source_url": "IR page or calendar url or null",\n'
        '    "notes": "one short sentence"\n'
        "  }\n"
        "}\n"
        "News rules: max 10 items, newest first, only include items published in the date window.\n"
        "Earnings rules: use the most recent completed earnings release (not an older quarter), and prefer IR/company release pages. "
        "If an earnings release occurred within the news window, set last_earnings_report_date to that release date and do not leave last_earnings_outcome as unknown. "
        "Outcome: 'beat' if EPS/revenue beat consensus, 'miss' if below, 'in_line' if roughly met."
    )

    payload = {
        "tools": [{"type": "web_search_preview"}],
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "max_output_tokens": 2200,
    }

    resp = responses_with_fallback(payload, api_key, model)
    text = extract_output_text(resp)

    try:
        parsed = parse_json_from_text(text)
    except json.JSONDecodeError:
        parsed = {}

    # Handle GPT returning a plain array instead of the expected object
    if isinstance(parsed, list):
        raw_news = parsed
        raw_earnings: Dict[str, Any] = {}
    elif isinstance(parsed, dict):
        raw_news = parsed.get("news", [])
        raw_earnings = parsed.get("earnings", {})
        if not isinstance(raw_news, list):
            raw_news = []
        if not isinstance(raw_earnings, dict):
            raw_earnings = {}
    else:
        raw_news = []
        raw_earnings = {}

    # Normalize news
    normalized: List[Dict[str, Any]] = []
    for row in raw_news:
        if isinstance(row, dict):
            item = normalize_news_item(row, ticker)
            if item:
                normalized.append(item)

    print(f"    {ticker}: GPT returned {len(raw_news)} raw news → {len(normalized)} passed normalization")
    normalized.sort(key=lambda x: x.get("datetime") or "", reverse=True)
    news_items = normalized[:10]

    # Normalize earnings
    raw_tl = {
        "last_earnings_report_date": pick_value(raw_earnings, ["last_earnings_report_date", "last_report_date"], None),
        "last_earnings_label":       pick_value(raw_earnings, ["last_earnings_label", "last_quarter_label"], None),
        "last_earnings_outcome":     pick_value(raw_earnings, ["last_earnings_outcome", "earnings_outcome", "outcome"], "unknown"),
        "next_earnings_date":        pick_value(raw_earnings, ["next_earnings_date", "next_report_date"], None),
        "next_earnings_status":      pick_value(raw_earnings, ["next_earnings_status"], "unknown"),
        "source_url":                pick_value(raw_earnings, ["source_url", "url"], None),
        "notes":                     pick_value(raw_earnings, ["notes", "note"], None),
    }
    earnings_timeline = normalize_earnings_timeline(raw_tl, now)
    earnings_timeline = reconcile_earnings_timeline_with_news(earnings_timeline, news_items, now)

    return news_items, earnings_timeline


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

def run(mode: str, tickers_file: Path, site_data_dir: Path, data_fetch_mode: str):
    if mode != "weekly":
        raise ValueError("Only 'weekly' mode is supported. Daily mode has been removed.")

    fetch_mode = (data_fetch_mode or os.getenv("DATA_FETCH_MODE", "hybrid")).strip().lower()
    if fetch_mode not in {"hybrid", "free_only", "gpt_only"}:
        raise ValueError("DATA_FETCH_MODE must be one of: hybrid, free_only, gpt_only")
    api_key = os.getenv("OPENAI_API_KEY")
    if fetch_mode in {"hybrid", "gpt_only"} and not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable for GPT-enabled fetch mode")

    model = PRIMARY_MODEL
    ticker_configs = load_tickers(tickers_file)
    now = utc_now()

    current_path = site_data_dir / "current_news.json"
    archive_path = site_data_dir / "archive" / "news_archive.json"
    latest_path  = site_data_dir / "latest.json"
    alerts_path  = site_data_dir / "alerts.json"

    current_news = read_json(current_path, [])
    archive_news = read_json(archive_path, [])

    all_new_items: List[Dict[str, Any]] = []
    holdings = []
    alert_items: List[Dict[str, Any]] = []

    for cfg in ticker_configs:
        ticker = cfg["ticker"]
        thesis = cfg.get("thesis")
        print(f"  Fetching {ticker}…")

        ticker_news: List[Dict[str, Any]] = []
        earnings_timeline = normalize_earnings_timeline({}, now)

        try:
            gpt_news: List[Dict[str, Any]] = []
            gpt_earnings = normalize_earnings_timeline({}, now)
            free_news: List[Dict[str, Any]] = []
            free_earnings = normalize_earnings_timeline({}, now)

            if fetch_mode in {"hybrid", "gpt_only"}:
                gpt_news, gpt_earnings = fetch_ticker_data_with_gpt(
                    ticker, now, api_key or "", model, thesis=thesis
                )
            if fetch_mode in {"hybrid", "free_only"}:
                free_news, free_earnings = fetch_ticker_data_from_free_sources(ticker, now)

            if fetch_mode == "gpt_only":
                ticker_news, earnings_timeline = gpt_news, gpt_earnings
            elif fetch_mode == "free_only":
                ticker_news, earnings_timeline = free_news, free_earnings
            else:
                # Compare and merge for higher confidence.
                ticker_news = merge_news_items_for_ticker(ticker, gpt_news + free_news)
                earnings_timeline = gpt_earnings
                gpt_next = gpt_earnings.get("next_earnings_date")
                free_next = free_earnings.get("next_earnings_date")
                gpt_out = gpt_earnings.get("last_earnings_outcome")
                free_out = free_earnings.get("last_earnings_outcome")
                consensus_bits: List[str] = [
                    f"source_mode={fetch_mode}",
                    f"gpt_news={len(gpt_news)}",
                    f"free_news={len(free_news)}",
                ]
                if gpt_next and free_next and gpt_next == free_next:
                    earnings_timeline["next_earnings_date"] = gpt_next
                    earnings_timeline["next_earnings_status"] = "scheduled"
                    consensus_bits.append("next_date_consensus=match")
                else:
                    # No cross-source agreement -> be explicit about uncertainty.
                    earnings_timeline["next_earnings_date"] = None
                    earnings_timeline["next_earnings_status"] = "unknown"
                    consensus_bits.append("next_date_consensus=none")

                if gpt_out and free_out and gpt_out == free_out and gpt_out != "unknown":
                    consensus_bits.append("outcome_consensus=match")
                else:
                    consensus_bits.append("outcome_consensus=mixed")

                note = (earnings_timeline.get("notes") or "").strip()
                earnings_timeline["notes"] = ("; ".join([note, "; ".join(consensus_bits)]).strip("; ")).strip() or None
                earnings_timeline = reconcile_earnings_timeline_with_news(earnings_timeline, ticker_news, now)
        except Exception as exc:
            msg = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in msg:
                raise RuntimeError(
                    "TLS certificate verification failed.\n"
                    "  pip install --upgrade certifi\n"
                    "  Or set OPENAI_CA_BUNDLE to your org CA PEM.\n"
                    "  Temp bypass: OPENAI_TLS_INSECURE=1"
                ) from exc
            print(f"    Warning: fetch failed for {ticker}; skipping. {msg}")

        all_new_items.extend(ticker_news)

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

    holdings = sorted(
        holdings,
        key=lambda x: (-x["thesis_breaking_count"], -x["fundamental_news_count"]),
    )

    tickers_with_changes = [h for h in holdings if h["fundamental_news_count"] > 0]
    durable_item_count   = len([n for n in all_new_items if n.get("horizon_fit") == "durable"])
    breaking_count       = len([n for n in all_new_items if n.get("thesis_signal") == "thesis_breaking"])

    latest = {
        "generated_at": iso(now),
        "mode": mode,
        "model": model,
        "data_fetch_mode": fetch_mode,
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

    save_weekly_snapshot(holdings, now, site_data_dir / "archive")

    breaking_tickers = [h["ticker"] for h in holdings if h["thesis_breaking_count"] > 0]
    print(
        f"\n✓ Done: {len(ticker_configs)} tickers · {len(all_new_items)} items · "
        f"{breaking_count} thesis-breaking"
        + (f" [{', '.join(breaking_tickers)}]" if breaking_tickers else "")
    )


def main():
    parser = argparse.ArgumentParser(description="Update stocks dashboard data (weekly)")
    parser.add_argument("--mode", choices=["weekly"], default="weekly")
    parser.add_argument("--tickers-file", default="config/tickers.json", type=Path)
    parser.add_argument("--site-data-dir", default="site/data", type=Path)
    parser.add_argument(
        "--data-fetch-mode",
        choices=["hybrid", "free_only", "gpt_only"],
        default=os.getenv("DATA_FETCH_MODE", "hybrid"),
        help="Data source strategy: hybrid compares GPT+free APIs, free_only uses SEC+GDELT+Alpha, gpt_only uses OpenAI web search only.",
    )
    args = parser.parse_args()
    run(args.mode, args.tickers_file, args.site_data_dir, args.data_fetch_mode)


if __name__ == "__main__":
    main()
