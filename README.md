# Portfolio Fundamental News Radar

A self-hosted, long-horizon stock intelligence dashboard. It runs on GitHub Actions, updates weekly data files in `site/data/`, and serves a static dashboard via GitHub Pages.

## Current Status

- Run mode in backend script: `weekly` only
- Scheduled workflow: `.github/workflows/stocks-weekly.yml` (Mondays 07:00 UTC)
- Default fetch mode in workflow: `hybrid`
- Default model: `gpt-4.1-mini`

## Data Fetch Modes

- `free_only`: free-source pipeline only
- `hybrid`: free-source pipeline first, GPT web-search fallback when free coverage is sparse/incomplete
- `gpt_only`: GPT web-search pipeline only

## Free-Source Pipeline

For free/hybrid mode, news is ingested in this order:

1. Brave Search
2. NewsAPI
3. SEC filings
4. GDELT

After merge/normalization, GPT classifier may refine/dedupe if `OPENAI_API_KEY` is available.

Earnings in free/hybrid are news-driven first; Alpha Vantage is used as confirmation/fallback metadata.

## Ticker Config

`config/tickers.json` supports:

Simple:
```json
{ "tickers": ["AAPL", "MSFT", "NVDA"] }
```

Extended (recommended):
```json
{
  "holdings": [
    { "ticker": "NVDA", "company": "NVIDIA", "thesis": "AI infrastructure supercycle" },
    { "ticker": "MSFT", "company": "Microsoft", "thesis": null },
    { "ticker": "PL", "company": "Planet Labs", "thesis": null }
  ]
}
```

`company` improves relevance filtering for ambiguous tickers.

## Required/Useful Secrets

Add in **Settings → Secrets and variables → Actions**:

- `OPENAI_API_KEY`
- `ALPHA_VANTAGE_API_KEY`
- `BRAVE_SEARCH_API_KEY`
- `NEWSAPI_API_KEY`
- `SEC_USER_AGENT`
- `OPENAI_INPUT_USD_PER_1M`
- `OPENAI_OUTPUT_USD_PER_1M`

## Workflow

Weekly workflow file:

- [stocks-weekly.yml](./.github/workflows/stocks-weekly.yml)

This job runs `python scripts/update_stocks.py --mode weekly` and commits updated `site/data/*.json`.

## Local Run

```bash
pip install certifi
export OPENAI_API_KEY=sk-...
export DATA_FETCH_MODE=hybrid
python scripts/update_stocks.py --mode weekly
```

## Dashboard URL

After enabling GitHub Pages (`main` branch, `/site` folder):

`https://YOUR-USERNAME.github.io/YOUR-REPO/stocks.html`
