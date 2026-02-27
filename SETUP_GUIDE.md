# Setup Guide (Current)

This guide matches the current repository behavior.

## 1. Repository + Pages

1. Create/clone your repo.
2. Ensure this structure exists:
   - `config/tickers.json`
   - `scripts/update_stocks.py`
   - `site/stocks.html`
   - `.github/workflows/stocks-weekly.yml`
3. Enable GitHub Pages:
   - Settings → Pages
   - Source: Deploy from branch
   - Branch: `main`
   - Folder: `/site`

## 2. Configure Tickers

Edit `config/tickers.json`.

Recommended extended format:

```json
{
  "holdings": [
    { "ticker": "RKLB", "company": "Rocket Lab USA", "thesis": null },
    { "ticker": "RDDT", "company": "Reddit", "thesis": null },
    { "ticker": "AXON", "company": "Axon Enterprise", "thesis": null }
  ]
}
```

Use `company` to improve exact-company relevance.

## 3. Add Actions Secrets

Add these in Settings → Secrets and variables → Actions:

- `OPENAI_API_KEY`
- `ALPHA_VANTAGE_API_KEY`
- `BRAVE_SEARCH_API_KEY`
- `NEWSAPI_API_KEY`
- `SEC_USER_AGENT`
- `OPENAI_INPUT_USD_PER_1M`
- `OPENAI_OUTPUT_USD_PER_1M`

For current `gpt-4.1-mini` pricing inputs:

- `OPENAI_INPUT_USD_PER_1M = 0.40`
- `OPENAI_OUTPUT_USD_PER_1M = 1.60`

## 4. Weekly Workflow

`stocks-weekly.yml` runs every Monday at 07:00 UTC and executes:

```bash
python scripts/update_stocks.py --mode weekly
```

Default env in workflow:

- `OPENAI_MODEL: gpt-4.1-mini`
- `DATA_FETCH_MODE: hybrid`

## 5. What the Pipeline Does

- Modes available in script: `weekly` only
- Fetch modes: `free_only`, `hybrid`, `gpt_only`
- Free/hybrid source order:
  1. Brave Search
  2. NewsAPI
  3. SEC
  4. GDELT
- GPT classifier runs after free-source merge when OpenAI key is present.
- Earnings are inferred from news first; Alpha is used for confirmation/fallback metadata.

## 6. Verify

1. Go to Actions tab
2. Run `Weekly Stock Update`
3. Confirm new files in `site/data/`
4. Open `https://YOUR-USERNAME.github.io/YOUR-REPO/stocks.html`

## 7. Cost Summary on Dashboard

The dashboard now shows per-week cost summary at the bottom:

- `free_only`: `free search = $0`
- `hybrid/gpt_only`: estimated request + GPT token costs from run metadata

Token-cost estimate uses `OPENAI_INPUT_USD_PER_1M` and `OPENAI_OUTPUT_USD_PER_1M`.
