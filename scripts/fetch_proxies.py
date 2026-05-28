"""Fetch and validate free HTTP/SOCKS proxies for the FTC submitter.

Keeps testing batches until working endpoints are found (``--until N``).

WARNING: Free public proxies are untrusted. They can read or modify traffic.

Usage:
    python scripts/fetch_proxies.py --until 5
    python scripts/fetch_proxies.py --test 100 --save 10 --country ALL --socks5
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

log = logging.getLogger(__name__)

PROXIFLY_BASE = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols"
SOURCES = ("http", "https", "socks4", "socks5")
PROXYSCRAPE_URL = (
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"
)

# Cloudflare-fronted "proxies" that never work for real egress.
_JUNK_HOST_PREFIXES = (
    "103.21.244.",
    "141.101.",
    "172.64.",
    "172.65.",
    "172.66.",
    "172.67.",
    "188.114.",
    "108.162.",
)


def _fetch_json(url: str) -> list[dict]:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def _is_junk(entry: dict) -> bool:
    ip = (entry.get("ip") or "").strip()
    if any(ip.startswith(p) for p in _JUNK_HOST_PREFIXES):
        return True
    port = entry.get("port")
    if port == 80:
        return True  # almost always CDN fronts, not real proxies
    return False


def _rank(entry: dict) -> tuple:
    https = 1 if entry.get("https") else 0
    score = int(entry.get("score") or 0)
    anon = entry.get("anonymity") or ""
    anon_rank = {"elite": 3, "anonymous": 2, "transparent": 1}.get(anon, 0)
    country = (entry.get("geolocation") or {}).get("country") or ""
    us_bonus = 1 if country.upper() == "US" else 0
    port = int(entry.get("port") or 0)
    port_bonus = 1 if port in (3128, 8080, 8888, 8000, 1080, 9050) else 0
    return (https, score, anon_rank, us_bonus, port_bonus)


def _fetch_proxyscrape() -> list[str]:
    """Plain-text proxy list from ProxyScrape (ip:port per line)."""
    try:
        with urllib.request.urlopen(PROXYSCRAPE_URL, timeout=60) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("Could not load ProxyScrape: %s", exc)
        return []
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        host, _, port = line.partition(":")
        if not host or not port.isdigit():
            continue
        if any(host.startswith(p) for p in _JUNK_HOST_PREFIXES):
            continue
        if int(port) == 80:
            continue
        out.append(f"http://{host}:{port}")
    random.shuffle(out)
    log.info("Loaded %d ProxyScrape HTTP proxies", len(out))
    return out


def _collect_candidates(
    *,
    country: str | None,
    protocols: tuple[str, ...],
    include_proxyscrape: bool = True,
) -> list[str]:
    rows: list[dict] = []
    for proto in protocols:
        url = f"{PROXIFLY_BASE}/{proto}/data.json"
        try:
            rows.extend(_fetch_json(url))
            log.info("Loaded %s list", proto)
        except Exception as exc:
            log.warning("Could not load %s: %s", proto, exc)

    if include_proxyscrape:
        # ProxyScrape uses ip:port — prepend so we test them early each run.
        scrape = _fetch_proxyscrape()
        rows.extend({"proxy": u, "ip": u.split("://")[-1].split(":")[0], "port": int(u.rsplit(":", 1)[-1])} for u in scrape)

    rows = [r for r in rows if not _is_junk(r)]

    if country:
        want = country.upper()
        filtered = [
            r
            for r in rows
            if (r.get("geolocation") or {}).get("country", "").upper() == want
        ]
        if filtered:
            rows = filtered

    rows.sort(key=_rank, reverse=True)

    # Shuffle within score tiers so we don't always hit the same dead IPs first.
    tiers: dict[tuple, list[dict]] = {}
    for row in rows:
        key = _rank(row)[:3]
        tiers.setdefault(key, []).append(row)
    shuffled: list[dict] = []
    for key in sorted(tiers.keys(), reverse=True):
        bucket = tiers[key][:]
        random.shuffle(bucket)
        shuffled.extend(bucket)

    seen: set[str] = set()
    out: list[str] = []
    for row in shuffled:
        url = (row.get("proxy") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _parse_proxy(proxy_url: str) -> dict | None:
    parsed = urlparse(proxy_url.strip())
    if not parsed.scheme or not parsed.hostname:
        return None
    port = parsed.port or (1080 if "socks" in parsed.scheme else 8080)
    out: dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{port}"}
    if parsed.username is not None:
        out["username"] = parsed.username
        out["password"] = parsed.password or ""
    return out


def _quick_reachable(proxy_url: str, *, timeout: float = 8.0) -> bool:
    """Fast check: can this proxy reach the public internet at all?"""
    proxy = _parse_proxy(proxy_url)
    if proxy is None:
        return False
    handlers = []
    server = proxy["server"]
    if proxy.get("username"):
        from urllib.parse import quote

        user = quote(proxy["username"])
        pw = quote(proxy.get("password") or "")
        hostpart = server.split("://", 1)[-1]
        scheme = server.split("://", 1)[0]
        proxy_url_full = f"{scheme}://{user}:{pw}@{hostpart}"
    else:
        proxy_url_full = server
    handlers.append(urllib.request.ProxyHandler({"http": proxy_url_full, "https": proxy_url_full}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open("http://api.ipify.org?format=text", timeout=timeout) as resp:
            ip = resp.read().decode().strip()
            return bool(ip and len(ip) >= 7)
    except Exception:
        return False


def _test_proxy(proxy_url: str, *, timeout_ms: int = 25000) -> bool:
    """Return True if donotcall.gov loads through this proxy."""
    if not _quick_reachable(proxy_url):
        return False

    proxy = _parse_proxy(proxy_url)
    assert proxy is not None

    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        from ftc_automation.ftc.ingest.gv_playwright import _resolve_chrome_executable

        with sync_playwright() as pw:
            chrome = _resolve_chrome_executable()
            launch_kw: dict = {
                "headless": True,
                "args": ["--disable-blink-features=AutomationControlled"],
                "ignore_default_args": ["--enable-automation"],
            }
            if chrome:
                launch_kw["executable_path"] = chrome
            else:
                launch_kw["channel"] = "chrome"
            browser = pw.chromium.launch(**launch_kw)
            context = browser.new_context(
                proxy=proxy,
                viewport={"width": 1366, "height": 900},
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            page.goto(
                "https://www.donotcall.gov/report.html",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            ok = (
                page.locator("#PhoneTextBox").count() > 0
                or page.locator("#MainContinueButton").count() > 0
            )
            body = page.locator("body").inner_text(timeout=4000).lower()
            if "system difficulties" in body or "unable to process" in body:
                ok = False
            context.close()
            browser.close()
            return ok
    except Exception as exc:
        log.debug("Proxy %s failed: %s", proxy_url, exc)
        return False


def _load_existing(out_path: Path) -> list[str]:
    if not out_path.exists():
        return []
    return [
        ln.strip()
        for ln in out_path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _save(out_path: Path, working: list[str]) -> None:
    header = (
        "# Auto-generated by scripts/fetch_proxies.py\n"
        "# WARNING: free public proxies are untrusted — see proxies.example.txt\n"
    )
    out_path.write_text(header + "\n".join(working) + "\n", encoding="utf-8")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Fetch + validate free proxies")
    parser.add_argument("--test", type=int, default=50, help="Candidates per batch")
    parser.add_argument("--save", type=int, default=15, help="Max total to save")
    parser.add_argument(
        "--until",
        type=int,
        default=3,
        help="Keep batching until this many working proxies found (0 = one batch only)",
    )
    parser.add_argument(
        "--country",
        default="ALL",
        help="Country filter (US, ALL, etc.)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel Playwright tests (keep low — heavy)",
    )
    parser.add_argument(
        "--out",
        default="ftc_automation/secrets/proxies.txt",
        help="Output file (gitignored)",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    working = _load_existing(out_path)
    already = set(working)

    country = None if args.country.upper() == "ALL" else args.country
    protocols = SOURCES

    log.info("Downloading proxy lists from Proxifly (%s)…", ", ".join(protocols))
    candidates = _collect_candidates(country=country, protocols=protocols)
    log.info("%d candidates after filtering junk CDN/port-80 entries", len(candidates))

    target = args.until if args.until > 0 else args.save
    if len(working) >= target:
        log.info("Already have %d proxy(ies) in %s", len(working), out_path)
        return 0

    offset = 0
    batch_num = 0
    tested_total = 0

    while len(working) < target and offset < len(candidates):
        batch_num += 1
        batch = candidates[offset : offset + args.test]
        if not batch:
            break
        offset += args.test
        log.info(
            "=== Batch %d: testing %d proxies (offset %d, found %d/%d) ===",
            batch_num,
            len(batch),
            offset - len(batch),
            len(working),
            target,
        )

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(_test_proxy, url): url for url in batch}
            done_in_batch = 0
            for fut in as_completed(futures):
                url = futures[fut]
                done_in_batch += 1
                tested_total += 1
                try:
                    ok = fut.result()
                except Exception as exc:
                    log.info("[%d] %s ✗ (%s)", done_in_batch, url, exc)
                    continue
                if ok and url not in already:
                    log.info("[%d] %s ✓ WORKS (donotcall.gov)", done_in_batch, url)
                    working.append(url)
                    already.add(url)
                    _save(out_path, working)
                    if len(working) >= target:
                        break
                elif ok is False:
                    log.info("[%d] %s ✗", done_in_batch, url)

        if args.until <= 0:
            break

    if working:
        _save(out_path, working)
        log.info(
            "Done. %d working proxy(ies) saved to %s (tested %d total).",
            len(working),
            out_path,
            tested_total,
        )
        return 0

    log.warning(
        "No working proxies after %d tests. Re-run later or use hotspot / paid proxy.",
        tested_total,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
