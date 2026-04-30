# robots-radar

Tracks which crawlers the web's top sites block. Fetches `robots.txt` from the Tranco top-1M, parses allow/disallow rules per user-agent, publishes a per-domain block map plus a block-rate time series.

## Run

```bash
pip install -r requirements.txt
python radar.py
```

Flags: `--top-thousands` (default 10), `--max-workers` (128), `--timeout` (3s).

## Output

- `domain-crawler-blocks.json` — `{domain: {blocked: [...], allowed: [...]}}`
- `crawler-block-percentages.json` — `{user_agent: {timestamp: rate}}`

## How

1. Download Tranco top-1M, take first N×1000.
2. Resolve DNS in parallel across 27 public resolvers (Cloudflare, Google, Quad9, …) → cache.
3. Patch `socket.getaddrinfo` → httpx fetches use cached IPs, original SNI preserved.
4. Fetch `https://{domain}/robots.txt` (one retry on failure).
5. Parse groups, classify each UA as blocked/allowed/mixed, aggregate.
6. Merge time series with prior local file or latest GitHub release.

## Releases

GitHub Actions runs daily, publishes both JSONs as release assets, prunes to 3.

```bash
wget https://github.com/tn3w/robots-radar/releases/latest/download/domain-crawler-blocks.json
wget https://github.com/tn3w/robots-radar/releases/latest/download/crawler-block-percentages.json
```
