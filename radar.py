#!/usr/bin/env python3
"""Build crawler block stats from top sites' robots.txt."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures as cf
import io
import json
import os
import re
import sys
import time
import zipfile

import warnings

import httpx

warnings.filterwarnings("ignore", category=Warning)

TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"
RELEASE_LATEST = "https://github.com/tn3w/robots-radar/releases/latest/download"
DOMAIN_FILE = "domain-crawler-blocks.json"
TIMESERIES_FILE = "crawler-block-percentages.json"
CRAWLERS_FILE = "crawler-stats.json"
USER_AGENT = "robots-radar/1.0 (+https://github.com/tn3w/robots-radar)"

DEFAULT_TIMEOUT = 3
DEFAULT_WORKERS = 512
DEFAULT_TOP_K = 100
TRANCO_TIMEOUT = 60
MAX_ROBOTS_BYTES = 512 * 1024
DIRECTIVE_RE = re.compile(r"(?i)(user-agent|allow|disallow|crawl-delay|sitemap)\s*:")
WHITESPACE_RE = re.compile(r"\s+")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def make_async_client(concurrency: int, timeout: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        verify=False,
        limits=httpx.Limits(
            max_connections=concurrency,
            max_keepalive_connections=concurrency // 2,
        ),
        timeout=httpx.Timeout(connect=10.0, read=timeout, write=timeout, pool=timeout),
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, br"},
    )


def download_top_domains(limit: int) -> list[str]:
    log(f"Downloading Tranco top-1M, taking first {limit:,}...")
    with httpx.Client(
        follow_redirects=True,
        timeout=TRANCO_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    ) as cli:
        payload = cli.get(TRANCO_URL)
        payload.raise_for_status()
    domains: list[str] = []
    with zipfile.ZipFile(io.BytesIO(payload.content)) as zf:
        with zf.open(zf.namelist()[0]) as handle:
            for line in io.TextIOWrapper(handle, encoding="utf-8"):
                parts = line.strip().split(",", 1)
                if len(parts) != 2:
                    continue
                domains.append(parts[1].strip().lower())
                if len(domains) >= limit:
                    break
    log(f"Loaded {len(domains):,} domains.")
    return domains


async def fetch_url(client: httpx.AsyncClient, url: str) -> str | None:
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            return ""
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_ROBOTS_BYTES:
                break
        return b"".join(chunks).decode("utf-8", errors="replace")


async def fetch_robots(client: httpx.AsyncClient, domain: str) -> str | None:
    for url in (f"https://{domain}/robots.txt", f"http://{domain}/robots.txt"):
        for attempt in range(2):
            try:
                return await fetch_url(client, url)
            except (httpx.HTTPError, OSError):
                if attempt == 0:
                    continue
                break
    return None


def split_directives(line: str) -> list[tuple[str, str]]:
    matches = list(DIRECTIVE_RE.finditer(line))
    if not matches:
        return []
    out: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        out.append((match.group(1).lower(), line[match.end() : end].strip()))
    return out


def group_state(rules: list[tuple[str, str]]) -> str | None:
    saw_allow = any(k == "allow" and v for k, v in rules)
    saw_disallow = any(k == "disallow" and v for k, v in rules)
    if saw_allow and saw_disallow:
        return "mixed"
    if saw_disallow:
        return "blocked"
    if saw_allow or rules:
        return "allowed"
    return None


class RobotsResult:
    __slots__ = ("states", "reason", "crawl_delays", "sitemaps", "wildcard_state")

    def __init__(
        self,
        states: dict[str, str],
        reason: str,
        crawl_delays: dict[str, float],
        sitemaps: list[str],
        wildcard_state: str | None,
    ) -> None:
        self.states = states
        self.reason = reason
        self.crawl_delays = crawl_delays
        self.sitemaps = sitemaps
        self.wildcard_state = wildcard_state


def parse_robots(text: str) -> RobotsResult:
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    sitemaps: list[str] = []
    saw_ua = saw_rule = False

    def flush() -> None:
        nonlocal agents, rules
        if agents:
            groups.append((agents, rules))
        agents, rules = [], []

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directives = split_directives(line)
        if not directives:
            field, value = line.split(":", 1)
            directives = [(field.strip().lower(), value.strip())]
        for key, value in directives:
            if key == "user-agent":
                saw_ua = True
                if rules:
                    flush()
                agents.append(value)
            elif key in {"allow", "disallow"}:
                saw_rule = True
                if agents:
                    rules.append((key, value))
            elif key == "crawl-delay":
                if agents:
                    rules.append((key, value))
            elif key == "sitemap" and value:
                sitemaps.append(value)
    flush()

    result: dict[str, str] = {}
    crawl_delays: dict[str, float] = {}

    for group_agents, group_rules in groups:
        state = group_state(
            [(k, v) for k, v in group_rules if k in {"allow", "disallow"}]
        )
        for rule_key, rule_val in group_rules:
            if rule_key == "crawl-delay":
                try:
                    delay = float(rule_val)
                    for agent in group_agents:
                        cleaned = WHITESPACE_RE.sub(
                            " ", agent.strip().strip('"').strip("'")
                        )
                        if cleaned:
                            crawl_delays[cleaned] = delay
                except ValueError:
                    pass

        if state is None:
            continue
        for agent in group_agents:
            cleaned = WHITESPACE_RE.sub(" ", agent.strip().strip('"').strip("'"))
            if not cleaned:
                continue
            existing = result.get(cleaned)
            result[cleaned] = (
                state
                if existing is None
                else (existing if existing == state else "mixed")
            )

    wildcard_state = result.get("*")
    keep = {k: v for k, v in result.items() if v in {"blocked", "allowed"}}
    if keep:
        return RobotsResult(keep, "ok", crawl_delays, sitemaps, wildcard_state)
    if not text.strip():
        return RobotsResult({}, "empty", {}, sitemaps, wildcard_state)
    if not saw_ua and not saw_rule:
        return RobotsResult({}, "no_directives", {}, sitemaps, wildcard_state)
    if saw_rule and not saw_ua:
        return RobotsResult({}, "orphan_rules", {}, sitemaps, wildcard_state)
    if saw_ua and not saw_rule:
        return RobotsResult({}, "ua_without_rules", {}, sitemaps, wildcard_state)
    return RobotsResult({}, "no_usable", {}, sitemaps, wildcard_state)


def all_allowed(states: dict[str, str]) -> bool:
    return bool(states) and set(states.values()) == {"allowed"}


class Accumulator:
    def __init__(self) -> None:
        self.blocked: int = 0
        self.allowed: int = 0
        self.mixed: int = 0
        self.crawl_delay_total: float = 0.0
        self.crawl_delay_count: int = 0

    def add_state(self, state: str) -> None:
        if state == "blocked":
            self.blocked += 1
        elif state == "allowed":
            self.allowed += 1
        elif state == "mixed":
            self.mixed += 1

    def add_crawl_delay(self, delay: float) -> None:
        self.crawl_delay_total += delay
        self.crawl_delay_count += 1


async def build_mapping(
    domains: list[str], concurrency: int, timeout: int
) -> tuple[
    dict[str, dict[str, list[str]]], dict[str, Accumulator], int, dict[str, int]
]:
    mapping: dict[str, dict[str, list[str]]] = {}
    crawler_acc: dict[str, Accumulator] = {}
    global_counts: dict[str, int] = {
        "sitemap": 0,
        "wildcard_blocked": 0,
        "wildcard_allowed": 0,
    }
    stats = {
        "fetch_failed": 0,
        "empty": 0,
        "no_directives": 0,
        "orphan_rules": 0,
        "ua_without_rules": 0,
        "no_usable": 0,
        "analyzed": 0,
    }
    total = len(domains)
    done = 0
    log(f"Fetching robots.txt for {total:,} domains, concurrency={concurrency}...")

    loop = asyncio.get_running_loop()
    loop.set_default_executor(cf.ThreadPoolExecutor(max_workers=concurrency))
    sem = asyncio.Semaphore(concurrency)

    async with make_async_client(concurrency, timeout) as cli:

        async def worker(domain: str) -> tuple[str, str | None]:
            async with sem:
                return domain, await fetch_robots(cli, domain)

        tasks = [asyncio.create_task(worker(d)) for d in domains]
        for coro in asyncio.as_completed(tasks):
            domain, text = await coro
            done += 1
            if done % 100 == 0 or done == total:
                log(f"Robots: {done:,}/{total:,} {stats} saved={len(mapping):,}")
            if text is None:
                stats["fetch_failed"] += 1
                continue

            result = parse_robots(text)
            if not result.states:
                stats[result.reason] = stats.get(result.reason, 0) + 1
                continue

            stats["analyzed"] += 1

            if result.sitemaps:
                global_counts["sitemap"] += 1
            if result.wildcard_state == "blocked":
                global_counts["wildcard_blocked"] += 1
            elif result.wildcard_state == "allowed":
                global_counts["wildcard_allowed"] += 1

            for pattern, state in result.states.items():
                crawler_acc.setdefault(pattern, Accumulator()).add_state(state)

            for agent, delay in result.crawl_delays.items():
                crawler_acc.setdefault(agent, Accumulator()).add_crawl_delay(delay)

            if all_allowed(result.states):
                continue
            blocked = sorted(p for p, s in result.states.items() if s == "blocked")
            allowed = sorted(p for p, s in result.states.items() if s == "allowed")
            if blocked or allowed:
                mapping[domain] = {"blocked": blocked, "allowed": allowed}

    log(f"Done robots: {stats} saved_domains={len(mapping):,}")
    global_counts.update(stats)
    global_counts["total"] = total
    return (
        dict(sorted(mapping.items())),
        crawler_acc,
        stats["analyzed"],
        global_counts,
    )


def build_crawler_stats(
    acc: dict[str, Accumulator], analyzed: int, global_counts: dict[str, int]
) -> dict:
    if analyzed == 0:
        return {}
    crawlers: dict[str, dict] = {}
    for agent, a in sorted(acc.items()):
        if a.blocked + a.allowed + a.mixed == 0:
            continue
        avg_delay = (
            round(a.crawl_delay_total / a.crawl_delay_count, 2)
            if a.crawl_delay_count > 0
            else None
        )
        crawlers[agent] = {
            "block_rate": round(a.blocked / analyzed, 6),
            "blocked": a.blocked,
            "allowed": a.allowed,
            "mixed": a.mixed,
            **({"avg_crawl_delay": avg_delay} if avg_delay is not None else {}),
        }
    return {"meta": {**global_counts, "analyzed": analyzed}, "crawlers": crawlers}


def percentages(acc: dict[str, Accumulator], analyzed: int) -> dict[str, float]:
    if analyzed == 0:
        return {}
    return {
        agent: round(a.blocked / analyzed, 6)
        for agent, a in acc.items()
        if a.blocked > 0
    }


def load_json(path: str) -> object:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, data: object) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), sort_keys=True)
        f.write("\n")


def normalize_timeseries(data: object) -> dict[str, dict[str, float]]:
    if not isinstance(data, dict):
        return {}
    return {
        str(k): {str(ts): float(v) for ts, v in hist.items()}
        for k, hist in data.items()
        if isinstance(hist, dict)
    }


def fetch_release_asset(name: str) -> bytes | None:
    try:
        with httpx.Client(
            follow_redirects=True, timeout=30, headers={"User-Agent": USER_AGENT}
        ) as cli:
            response = cli.get(f"{RELEASE_LATEST}/{name}")
            if response.status_code != 200:
                return None
            return response.content
    except (httpx.HTTPError, OSError):
        return None


def load_timeseries(path: str) -> dict[str, dict[str, float]]:
    if os.path.exists(path):
        log(f"Loading time series from {path}...")
        return normalize_timeseries(load_json(path))
    log("No local time series; checking latest release...")
    blob = fetch_release_asset(TIMESERIES_FILE)
    if blob is None:
        log("None found; starting fresh.")
        return {}
    try:
        return normalize_timeseries(json.loads(blob.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def update_timeseries(
    existing: dict[str, dict[str, float]], pct: dict[str, float], ts: int
) -> dict[str, dict[str, float]]:
    out = {k: dict(v) for k, v in existing.items()}
    key = str(ts)
    for pattern, value in pct.items():
        out.setdefault(pattern, {})[key] = value
    return dict(sorted(out.items()))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build crawler block stats from robots.txt."
    )
    p.add_argument("--top-thousands", type=int, default=DEFAULT_TOP_K)
    p.add_argument("--max-workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--domain-output", default=DOMAIN_FILE)
    p.add_argument("--timeseries-output", default=TIMESERIES_FILE)
    p.add_argument("--crawlers-output", default=CRAWLERS_FILE)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    limit = max(1, args.top_thousands) * 1000
    domains = download_top_domains(limit)

    mapping, crawler_acc, analyzed, global_counts = asyncio.run(
        build_mapping(domains, args.max_workers, args.timeout)
    )
    log(f"Writing {args.domain_output}...")
    write_json(args.domain_output, mapping)

    crawler_stats = build_crawler_stats(crawler_acc, analyzed, global_counts)
    log(f"Writing {args.crawlers_output}: {len(crawler_stats['crawlers']):,} crawlers")
    write_json(args.crawlers_output, crawler_stats)

    existing = load_timeseries(args.timeseries_output)
    pct = percentages(crawler_acc, analyzed)
    ts = int(time.time())
    updated = update_timeseries(existing, pct, ts)
    log(f"Writing {args.timeseries_output}: {len(pct):,} crawlers @ {ts}")
    write_json(args.timeseries_output, updated)
    log("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
