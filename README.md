# Portfolio Fundamental News Radar

A self-hosted, long-horizon stock intelligence dashboard. Runs on GitHub Actions, fetches fundamental news via GPT with web search, and publishes a static site you can embed anywhere — including WordPress.

**Philosophy:** You hold stocks for 2+ years. Short-term price moves and analyst upgrades are noise. This tool surfaces only news that could change the fundamental story — things you'd actually need to act on.

---

## How It Works

| Mode | Schedule | What it does |
|------|----------|-------------|
| **Weekly** | Every Monday 6am UTC | Broad 7-day sweep. Fetches all fundamental news, updates `latest.json`, saves a frozen weekly snapshot. |
| **Daily** | Mon–Fri 7am UTC | Aggressive 24h scan. Only looks for thesis-breaking or thesis-confirming events. Writes `alerts.json`. If nothing material happened, writes nothing. |

---

## Project Structure

```
.
├── config/
│   └── tickers.json             # Your holdings (simple list or with thesis notes)
├── scripts/
│   └── update_stocks.py         # Core pipeline
├── site/
│   ├── stocks.html              # Dashboard UI
│   └── data/
│       ├── latest.json          # Latest weekly data
│       ├── alerts.json          # Rolling 30-day thesis-level alerts
│       ├── current_news.json    # Active news window (14 days)
│       └── archive/
│           ├── news_archive.json        # Rolling 1-year archive
│           └── week_YYYY-MM-DD.json    # Frozen weekly snapshots
└── .github/workflows/
    ├── stocks-daily.yml
    └── stocks-weekly.yml
```

---

## Setup

### 1. Fork or clone this repo

### 2. Add your OpenAI API key to GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**

- Name: `OPENAI_API_KEY`
- Value: your key (needs `gpt-4o-search-preview` or `gpt-4.1-mini`)

### 3. Configure your tickers

Edit `config/tickers.json`. Two formats are supported:

**Simple:**
```json
{ "tickers": ["AAPL", "MSFT", "NVDA", "TSLA"] }
```

**Extended (with optional thesis notes — strongly recommended):**
```json
{
  "holdings": [
    { "ticker": "NVDA", "thesis": "AI infrastructure supercycle, GPU monopoly for data centers" },
    { "ticker": "MSFT", "thesis": "Azure cloud + Copilot AI monetization across enterprise" },
    { "ticker": "AAPL", "thesis": null }
  ]
}
```

When `thesis` is `null`, GPT uses general knowledge of why investors hold that stock. Adding your own thesis makes breaking-news detection much more precise — see the [Adding Thesis Notes](#adding-thesis-notes) section.

### 4. Enable GitHub Pages

**Settings → Pages → Source: Deploy from a branch → Branch: main → Folder: /site**

Your dashboard: `https://YOUR-USERNAME.github.io/YOUR-REPO/stocks.html`

### 5. Test locally (optional)

```bash
pip install certifi
export OPENAI_API_KEY=sk-...
python scripts/update_stocks.py --mode weekly
# open site/stocks.html in browser
```

---

## GitHub Actions

Both workflows trigger automatically once pushed. You can also trigger them manually via the Actions tab.

**Weekly** (`stocks-weekly.yml`) — every Monday 6am UTC:
```yaml
on:
  schedule:
    - cron: '0 6 * * 1'
  workflow_dispatch:
```

**Daily** (`stocks-daily.yml`) — weekdays 7am UTC:
```yaml
on:
  schedule:
    - cron: '0 7 * * 1-5'
  workflow_dispatch:
```

Both workflows commit the updated `site/data/*.json` back to the repo, which automatically triggers a GitHub Pages redeploy.

---

## Embedding in WordPress

Since GitHub Pages serves your `site/` as a static website, embed it with a single iframe.

**Simple iframe** (paste into an HTML block in WordPress):
```html
<iframe
  src="https://YOUR-USERNAME.github.io/YOUR-REPO/stocks.html"
  width="100%"
  height="900"
  frameborder="0"
  style="border-radius:12px; background:#0a0d12;"
  title="Portfolio Radar"
></iframe>
```

**Responsive version:**
```html
<div style="position:relative; width:100%; padding-bottom:80vh;">
  <iframe
    src="https://YOUR-USERNAME.github.io/YOUR-REPO/stocks.html"
    style="position:absolute; top:0; left:0; width:100%; height:100%; border:none; border-radius:12px;"
    title="Portfolio Radar"
  ></iframe>
</div>
```

The dashboard fetches its data from GitHub Pages every time it loads (no cache), so the WordPress page always shows the latest data — no WordPress plugin or database changes needed.

---

## Thesis Signals

Every news item gets a `thesis_signal`:

| Signal | Meaning | What to do |
|--------|---------|-----------|
| `thesis_breaking` ⚡ | The fundamental story has changed negatively — regulatory ban, structural disruption, fraud, key business lost | Review position seriously |
| `thesis_confirming` ✓ | Story is strengthening — record wins, major new market, moat-widening deal | Hold or add with confidence |
| `noise` | Relevant news but doesn't change the long-term picture | Monitor only |

Cards with breaking signals are highlighted in red and sorted to the top. A global alert bar appears if any ticker has breaking news.

---

## Archive Strategy

| File | Contents | Retention |
|------|---------|-----------|
| `current_news.json` | Last 14 days of news | Rolling |
| `alerts.json` | Thesis-level alerts | Rolling 30 days |
| `news_archive.json` | News older than 14 days | 1 year |
| `week_YYYY-MM-DD.json` | Complete frozen weekly snapshot | Permanent |

Weekly snapshots let you look back: "What was the state of my portfolio the week that regulation passed?"

---

## Adding Thesis Notes

Adding thesis notes is the single biggest upgrade you can make. Instead of GPT guessing your reason for holding, you tell it:

```json
{
  "ticker": "NVDA",
  "thesis": "AI infrastructure supercycle — CUDA moat, Blackwell/Rubin GPU monopoly for hyperscaler data centers, NIM software layer creating recurring revenue"
}
```

Now when AMD announces competitive AI chips at 60% of Nvidia's price, GPT correctly flags it as `thesis_breaking` because it directly threatens the GPU monopoly assumption. Without the note, it might tag it as routine "Product / Strategy" noise.

Write the thesis in your own words — there's no required format.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | Yes | — | Your OpenAI API key |
| `OPENAI_MODEL` | No | `gpt-4o-search-preview` | Primary model (falls back to `gpt-4.1-mini`) |
| `OPENAI_TLS_INSECURE` | No | — | Set `1` to skip TLS verification (local dev only) |
| `OPENAI_CA_BUNDLE` | No | — | Path to custom CA PEM (corporate networks) |
