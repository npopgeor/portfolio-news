# Setting Up Your Portfolio Dashboard on GitHub

Follow these steps exactly. Takes about 15–20 minutes total.

---

## PART 1 — Create the GitHub repository

### Step 1: Create a GitHub account (skip if you have one)
Go to https://github.com and sign up. Free account is fine.

### Step 2: Create a new repository
1. Click the **+** icon top-right → **New repository**
2. Name it: `portfolio-news` (or anything you like)
3. Set it to **Public** ← required for free GitHub Pages
4. Check **Add a README file**
5. Click **Create repository**

---

## PART 2 — Upload your project files

### Step 3: Create the folder structure
In your new repo, you need these files and folders:

```
portfolio-news/
├── config/
│   └── tickers.json
├── scripts/
│   └── update_stocks.py
├── site/
│   ├── stocks.html
│   └── data/
│       └── .gitkeep          ← empty file so git tracks the folder
├── .github/
│   └── workflows/
│       ├── stocks-weekly.yml
│       └── stocks-daily.yml
└── README.md
```

### Step 4: Upload files via GitHub web interface
1. In your repo, click **Add file → Upload files**
2. Drag and drop your files, or use the file picker
3. For folders: GitHub web doesn't support folder creation directly.
   Use this trick: click **Add file → Create new file**, type `config/tickers.json`
   and GitHub will create the folder automatically.

**Easier option — use GitHub Desktop (recommended for beginners):**
1. Download https://desktop.github.com
2. Sign in and clone your new repository to your computer
3. Copy all your project files into the cloned folder
4. In GitHub Desktop: commit and push

### Step 5: Create the workflow files

Create `.github/workflows/stocks-weekly.yml`:

```yaml
name: Weekly Stock Update

on:
  schedule:
    - cron: '0 7 * * 1'   # Every Monday at 07:00 UTC
  workflow_dispatch:        # Allows manual trigger from GitHub UI

jobs:
  update:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install certifi

      - name: Run weekly update
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          OPENAI_MODEL: gpt-4o-search-preview
        run: python scripts/update_stocks.py --mode weekly

      - name: Commit updated data
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add site/data/
          git diff --staged --quiet || git commit -m "Weekly data update $(date -u +%Y-%m-%d)"
          git push
```

Create `.github/workflows/stocks-daily.yml`:

```yaml
name: Daily Alert Check

on:
  schedule:
    - cron: '30 8 * * 1-5'  # Mon–Fri at 08:30 UTC
  workflow_dispatch:

jobs:
  alert-check:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install certifi

      - name: Run daily alert check
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          OPENAI_MODEL: gpt-4o-search-preview
        run: python scripts/update_stocks.py --mode daily

      - name: Commit if changes
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add site/data/
          git diff --staged --quiet || git commit -m "Daily alert check $(date -u +%Y-%m-%dT%H:%M)"
          git push
```

---

## PART 3 — Add your OpenAI API key as a secret

This is the most important step — without it, the automation won't work.

### Step 6: Add the secret
1. In your GitHub repo, click **Settings** (top tab)
2. In the left sidebar: **Secrets and variables → Actions**
3. Click **New repository secret**
4. Name: `OPENAI_API_KEY`
5. Value: your OpenAI key (starts with `sk-...`)
   Get one at: https://platform.openai.com/api-keys
6. Click **Add secret**

Your key is now encrypted. GitHub Actions can use it but nobody can read it.

---

## PART 4 — Enable GitHub Pages (your public website)

### Step 7: Enable Pages
1. In your repo → **Settings**
2. Left sidebar: **Pages**
3. Under "Build and deployment":
   - Source: **Deploy from a branch**
   - Branch: `main`
   - Folder: `/site`
4. Click **Save**

GitHub will build and deploy your site. Wait 1–2 minutes.

### Step 8: Find your URL
Your dashboard is now live at:
```
https://YOUR-USERNAME.github.io/portfolio-news/stocks.html
```
Replace `YOUR-USERNAME` with your GitHub username.

---

## PART 5 — Test it

### Step 9: Trigger a manual run
1. Go to your repo → **Actions** tab
2. Click **Weekly Stock Update** in the left list
3. Click **Run workflow → Run workflow** (green button)
4. Watch it run — takes 2–5 minutes
5. When done, refresh your GitHub Pages URL

If it fails, click the failed run to see the error log.

---

## PART 6 — Add to WordPress

### Step 10: Embed in WordPress
1. Log into WordPress → create or edit a page
2. Add a **Custom HTML** block
3. Paste this:

```html
<iframe
  src="https://YOUR-USERNAME.github.io/portfolio-news/stocks.html"
  width="100%"
  height="950"
  style="border:none;"
  title="Portfolio Intelligence">
</iframe>
```

4. Publish the page

---

## What runs automatically after setup

| When | What happens |
|------|-------------|
| Every Monday 07:00 UTC | Full weekly news sweep for all tickers, saves `latest.json` + weekly snapshot |
| Mon–Fri 08:30 UTC | Scans last 36 hours for thesis-breaking news only, updates `alerts.json` |
| After each run | GitHub automatically re-deploys the updated site |

You don't need to do anything — just check your dashboard.

---

## How to add or remove tickers

Edit `config/tickers.json` in GitHub (click the file → pencil icon to edit):

```json
{
  "holdings": [
    { "ticker": "AAPL" },
    { "ticker": "MSFT" },
    { "ticker": "NVDA" },
    { "ticker": "TSLA" },
    { "ticker": "AXON" }
  ]
}
```

Commit the change. The next scheduled run will pick up the new tickers.

---

## Troubleshooting

**Run fails with "Missing OPENAI_API_KEY"**
→ Check Step 6. The secret name must be exactly `OPENAI_API_KEY`.

**Site shows old data after a run**
→ GitHub Pages caches aggressively. Hard refresh: Ctrl+Shift+R (Windows) or Cmd+Shift+R (Mac).

**Run fails with TLS error on macOS local test**
→ Run: `pip install --upgrade certifi`

**"No such file or directory: site/data"**
→ Make sure the `site/data/` folder exists with a `.gitkeep` file inside it.
