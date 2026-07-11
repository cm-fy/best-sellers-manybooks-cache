# Best-Sellers ManyBooks Cache

Static cached ManyBooks category list data, updated weekly via GitHub Actions.

## Why a cache?

ManyBooks category pages (`https://manybooks.net/categories/<CODE>`) are served
behind a Cloudflare challenge that blocks scripted clients.  The Best-Sellers
calibre plugin therefore reads these lists from this static mirror instead of
fetching the live pages directly.  The **Latest releases** feed
(`https://manybooks.net/rss.xml`) is fetched live by the plugin because the RSS
endpoint is not Cloudflare-protected and its items embed direct EPUB download
links.

## Data format

- `mb/meta.json` — metadata about all cached category lists
- `mb/<slug>.json` — one file per category (e.g. `mb/non.json`)
- `mb/non_downloads.json` — Non-fiction sorted by downloads

Each book object uses the schema the plugin expects:

```json
{
  "rank": "1",
  "title": "The Grammar of English Grammars",
  "authors": "Goold Brown",
  "cover_url": "https://manybooks.net/sites/default/files/styles/220x330sc/public/...jpg",
  "source_url": "https://manybooks.net/titles/...",
  "manybooks_download_url": "https://manybooks.net/sites/default/files/.../...epub",
  "manybooks_format": "EPUB"
}
```

## Usage

GitHub Pages URL pattern:

```
https://cm-fy.github.io/best-sellers-manybooks-cache/mb/{slug}.json
```

## Data freshness

Lists are fetched every Monday at 03:00 UTC.  Manual triggers are also available
via the Actions tab.  When a fetch is blocked (Cloudflare/captcha), an empty JSON
array is written so the plugin can fall back to direct fetching.

## Running locally

```bash
pip install requests beautifulsoup4 lxml playwright
playwright install chromium
# optional: export a warmed Cloudflare cookie
export MB_COOKIE="cf_clearance=...; ..."
python3 fetch_manybooks.py
```
