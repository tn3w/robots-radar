# robots-radar

Tracks which crawlers the web's top sites block. Fetches `robots.txt` from the Tranco top-1M, parses allow/disallow rules per user-agent, and publishes three JSON files as GitHub release assets.

## Run

```bash
pip install -r requirements.txt
python radar.py
```

Flags: `--top-thousands` (default 20), `--max-workers` (256), `--timeout` (3s).

## Output

All files are **minified JSON**.

### `domain-crawler-blocks.json`
Per-domain crawler map:
```json
{"example.com":{"blocked":["GPTBot"],"allowed":["Googlebot"]}}
```

### `crawler-stats.json`
Aggregated per-crawler stats across all analyzed domains:
```json
{
  "GPTBot": {
    "block_rate": 0.312,
    "blocked": 3120,
    "allowed": 450,
    "mixed": 30,
    "wildcard_blocked": 1200,
    "wildcard_allowed": 800,
    "avg_crawl_delay": 5.0
  }
}
```

Fields:
| Field | Description |
|---|---|
| `block_rate` | fraction of analyzed domains that explicitly block this crawler |
| `blocked` / `allowed` / `mixed` | raw domain counts |
| `wildcard_blocked` / `wildcard_allowed` | domains where `*` also blocks/allows (context for inherited rules) |
| `avg_crawl_delay` | mean `Crawl-delay` across domains that set it for this crawler |

### `crawler-block-percentages.json`
Block-rate time series for trend tracking:
```json
{"GPTBot":{"1746000000":0.312}}
```

## How

1. Download Tranco top-1M, take first N×1000.
2. Fetch `https://{domain}/robots.txt` in parallel.
3. Parse groups — classify each UA as blocked/allowed/mixed, extract crawl-delay and sitemaps.
4. Accumulate per-crawler stats including `*` wildcard coverage.
5. Merge time series with prior local file or latest GitHub release.

## Releases

GitHub Actions runs daily, publishes all three JSONs as release assets, prunes to 3 releases.

```bash
wget https://github.com/tn3w/robots-radar/releases/latest/download/domain-crawler-blocks.json
wget https://github.com/tn3w/robots-radar/releases/latest/download/crawler-block-percentages.json
wget https://github.com/tn3w/robots-radar/releases/latest/download/crawler-stats.json
```
