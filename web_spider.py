#!/usr/bin/env python3
"""
site_health_spider.py

Crawls a site from its homepage, follows every internal link it finds,
and reports every URL that returns a 4xx or 5xx status -- along with
which page(s) are linking to it (so you know where to fix the anchor).

Also flags:
  - noindex pages (soft signal)
  - Next.js error-fallback pages (200 that are secretly broken)
  - Redirect chains longer than 1 hop (slow/fragile)
  - Pages missing from sitemap.xml

Output:
  <domain>_404_report.csv   -- one row per broken URL
  <domain>_all_pages.csv    -- every URL crawled, with status
  <domain>_noindex.csv      -- 200 pages marked noindex
  <domain>_orphans.csv      -- pages with no internal inlinks
  <domain>_external_links.csv -- status of all outbound links

Usage:
    pip install requests beautifulsoup4
    python site_health_spider.py https://example.com
    python site_health_spider.py https://example.com --workers 10 --max-urls 2000
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, urldefrag

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests beautifulsoup4")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
NEXT_ERR = re.compile(r"NEXT_HTTP_ERROR_FALLBACK|__next_error__", re.I)
NOINDEX  = re.compile(
    r'<meta[^>]+name=["\']robots["\'][^>]*content=["\'][^"\']*noindex', re.I
)
CANONICAL = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', re.I
)


def make_session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.4,
                  status_forcelist=[500, 502, 503, 504],
                  allowed_methods=["GET"])
    ad = HTTPAdapter(max_retries=retry, pool_maxsize=40)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


def normalise(url, base):
    url, _ = urldefrag(url)
    url = url.rstrip("?")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", ""):
        return None
    full = urljoin(base, url)
    p = urlparse(full)
    return f"{p.scheme}://{p.netloc}{p.path}" if not p.query else full


def same_host(url, host):
    return urlparse(url).netloc.lstrip("www.") == host.lstrip("www.")


def fetch_sitemap(base, session, timeout):
    urls = set()
    try:
        r = session.get(urljoin(base, "/sitemap.xml"), timeout=timeout,
                        headers={"User-Agent": BROWSER_UA})
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text, re.I):
            urls.add(loc.strip())
        for sub in re.findall(r"<sitemap>\s*<loc>\s*([^<\s]+)\s*</loc>", r.text, re.I):
            try:
                rs = session.get(sub.strip(), timeout=timeout,
                                 headers={"User-Agent": BROWSER_UA})
                for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", rs.text, re.I):
                    urls.add(loc.strip())
            except Exception:
                pass
    except Exception:
        pass
    return urls


def crawl_one(url, session, timeout):
    result = {
        "url": url, "status": None, "final_url": url,
        "redirects": 0, "x_robots": "", "noindex": False,
        "next_error": False, "canonical": "", "links": [],
        "error": ""
    }
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True,
                        headers={"User-Agent": BROWSER_UA})
        html = r.text or ""
        result["status"]     = r.status_code
        result["final_url"]  = r.url
        result["redirects"]  = len(r.history)
        result["x_robots"]   = r.headers.get("X-Robots-Tag", "")
        result["noindex"]    = (bool(NOINDEX.search(html)) or
                                "noindex" in result["x_robots"].lower())
        result["next_error"] = bool(NEXT_ERR.search(html))
        m = CANONICAL.search(html)
        result["canonical"]  = m.group(1) if m else ""

        if r.status_code == 200 and "text/html" in r.headers.get("Content-Type", ""):
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.find_all("a", href=True):
                href = tag["href"].strip()
                if href:
                    result["links"].append(href)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def spider(start_url, max_urls, workers, timeout):
    session = make_session()
    host    = urlparse(start_url).netloc
    base    = f"{urlparse(start_url).scheme}://{host}"

    print(f"[i] Fetching sitemap …")
    sitemap_urls = fetch_sitemap(base, session, timeout)
    print(f"[i] Sitemap has {len(sitemap_urls)} entries")

    queue   = {start_url} | sitemap_urls
    visited = {}
    inlinks   = defaultdict(set)
    ext_links = defaultdict(set)

    print(f"[i] Starting spider (max {max_urls} URLs, {workers} workers) …\n")

    while queue and len(visited) < max_urls:
        batch = list(queue - set(visited.keys()))[:min(workers * 4, max_urls - len(visited))]
        if not batch:
            break
        queue -= set(batch)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(crawl_one, u, session, timeout): u for u in batch}
            for fut in as_completed(futs):
                u   = futs[fut]
                res = fut.result()
                res["in_sitemap"] = u in sitemap_urls or res["final_url"] in sitemap_urls
                visited[u] = res

                for href in res["links"]:
                    norm = normalise(href, res["final_url"])
                    if not norm:
                        continue
                    if same_host(norm, host):
                        if norm not in visited:
                            inlinks[norm].add(u)
                            queue.add(norm)
                    else:
                        ext_links[norm].add(u)

        done = len(visited)
        print(f"    crawled {done:,}  |  queue {len(queue):,}  |  "
              f"4xx/5xx so far: {sum(1 for v in visited.values() if isinstance(v['status'], int) and v['status'] >= 400):,}",
              end="\r")

    print(f"\n[i] Done. {len(visited):,} URLs crawled.")
    return visited, inlinks, sitemap_urls, ext_links


def write_reports(visited, inlinks, sitemap_urls, ext_links, domain, session, timeout):
    safe = domain.replace(".", "_").replace("/", "_")
    broken_path   = f"{safe}_404_report.csv"
    all_path      = f"{safe}_all_pages.csv"
    noindex_path  = f"{safe}_noindex.csv"
    orphan_path   = f"{safe}_orphans.csv"
    external_path = f"{safe}_external_links.csv"

    broken_cols = [
        "status", "url", "final_url", "redirects", "reason",
        "noindex", "next_error", "canonical", "in_sitemap",
        "linked_from_count", "linked_from_sample"
    ]
    all_cols = [
        "status", "url", "final_url", "redirects", "noindex",
        "next_error", "canonical", "in_sitemap", "x_robots", "error"
    ]
    noindex_cols  = ["status", "url", "final_url", "x_robots", "canonical", "in_sitemap"]
    orphan_cols   = ["status", "url", "final_url", "in_sitemap", "noindex", "canonical"]
    external_cols = ["status", "status_class", "url", "linked_from_count", "linked_from_sample"]

    broken_rows   = []
    all_rows      = []
    noindex_rows  = []
    orphan_rows   = []
    external_rows = []

    for url, r in sorted(visited.items(), key=lambda x: (x[1]["status"] or 999)):
        status  = r["status"]
        reasons = []
        if r["error"] and status is None:
            reasons.append("fetch_error")
        if isinstance(status, int) and status >= 400:
            reasons.append(f"http_{status}")
        if r["next_error"]:
            reasons.append("next_error_fallback")
        if r["noindex"]:
            reasons.append("noindex")
        if r["redirects"] > 1:
            reasons.append(f"redirect_chain_{r['redirects']}_hops")

        linkers = sorted(inlinks.get(url, []))

        all_rows.append({
            "status": status, "url": url,
            "final_url": r["final_url"], "redirects": r["redirects"],
            "noindex": r["noindex"], "next_error": r["next_error"],
            "canonical": r["canonical"], "in_sitemap": r["in_sitemap"],
            "x_robots": r["x_robots"], "error": r["error"]
        })

        is_broken = (
            (isinstance(status, int) and status >= 400) or
            r["next_error"] or
            (r["error"] and status is None)
        )
        if is_broken:
            broken_rows.append({
                "status": status, "url": url,
                "final_url": r["final_url"], "redirects": r["redirects"],
                "reason": "; ".join(reasons),
                "noindex": r["noindex"], "next_error": r["next_error"],
                "canonical": r["canonical"], "in_sitemap": r["in_sitemap"],
                "linked_from_count": len(linkers),
                "linked_from_sample": " | ".join(linkers[:5])
            })

        if r["noindex"] and not is_broken:
            noindex_rows.append({
                "status": status, "url": url,
                "final_url": r["final_url"], "x_robots": r["x_robots"],
                "canonical": r["canonical"], "in_sitemap": r["in_sitemap"],
            })

        is_orphan = (url not in inlinks or len(inlinks[url]) == 0)
        if is_orphan and not is_broken:
            orphan_rows.append({
                "status": status, "url": url,
                "final_url": r["final_url"], "in_sitemap": r["in_sitemap"],
                "noindex": r["noindex"], "canonical": r["canonical"],
            })

    broken_rows.sort(key=lambda x: (-(x["linked_from_count"]), str(x["status"])))
    noindex_rows.sort(key=lambda x: str(x["url"]))
    orphan_rows.sort(key=lambda x: (x["in_sitemap"], str(x["url"])))

    for path, cols, rows in [
        (broken_path,   broken_cols,   broken_rows),
        (all_path,      all_cols,      all_rows),
        (noindex_path,  noindex_cols,  noindex_rows),
        (orphan_path,   orphan_cols,   orphan_rows),
    ]:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader(); w.writerows(rows)

    # ── External links ────────────────────────────────────────────────
    print(f"\n[i] Checking {len(ext_links):,} unique external URLs …")

    def check_external(url):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True,
                            headers={"User-Agent": BROWSER_UA})
            return url, r.status_code
        except Exception as e:
            return url, f"ERR:{type(e).__name__}"

    total_ext = len(ext_links)
    done_ext  = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(check_external, u): u for u in ext_links}
        for fut in as_completed(futs):
            url, status = fut.result()
            done_ext += 1
            print(f"    external {done_ext}/{total_ext} ({done_ext*100//total_ext}%)", end="\r")
            linkers = sorted(ext_links[url])
            if isinstance(status, int):
                if status < 300:   cls = "2xx_ok"
                elif status < 400: cls = "3xx_redirect"
                elif status < 500: cls = "4xx_error"
                else:              cls = "5xx_error"
            else:
                cls = "fetch_error"
            external_rows.append({
                "status": status, "status_class": cls, "url": url,
                "linked_from_count": len(linkers),
                "linked_from_sample": " | ".join(linkers[:5])
            })
    print()

    external_rows.sort(key=lambda x: (x["status_class"], -x["linked_from_count"]))
    with open(external_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=external_cols)
        w.writeheader(); w.writerows(external_rows)

    return broken_rows, noindex_rows, orphan_rows, external_rows, \
           broken_path, noindex_path, orphan_path, external_path, all_path


def main():
    ap = argparse.ArgumentParser(
        description="Crawl a site and report broken links, noindex pages, orphans, and redirect chains."
    )
    ap.add_argument("url",          help="Start URL, e.g. https://example.com")
    ap.add_argument("--workers",    type=int, default=10)
    ap.add_argument("--max-urls",   type=int, default=1500)
    ap.add_argument("--timeout",    type=int, default=20)
    args = ap.parse_args()

    start  = args.url.rstrip("/")
    domain = urlparse(start).netloc

    visited, inlinks, sitemap_urls, ext_links = spider(
        start, args.max_urls, args.workers, args.timeout
    )
    session = make_session()
    (broken_rows, noindex_rows, orphan_rows, external_rows,
     broken_path, noindex_path, orphan_path, external_path, all_path) = write_reports(
        visited, inlinks, sitemap_urls, ext_links, domain, session, args.timeout
    )

    # ── Summary ───────────────────────────────────────────────────────
    total  = len(visited)
    ok     = sum(1 for v in visited.values()
                 if isinstance(v["status"], int) and v["status"] < 400
                 and not v["next_error"])
    redir  = sum(1 for v in visited.values() if v["redirects"] > 0)
    broken = len(broken_rows)

    print("\n" + "=" * 60)
    print("SITEWIDE HEALTH REPORT")
    print("=" * 60)
    print(f"Total URLs crawled ........ {total:,}")
    print(f"Clean (2xx, indexable) .... {ok:,}")
    print(f"Redirect (any) ............ {redir:,}")
    print(f"Noindex pages ............. {len(noindex_rows):,}")
    print(f"BROKEN (4xx/5xx/next_err) . {broken:,}  ← fix these")
    print(f"Orphan pages (0 inlinks) .. {len(orphan_rows):,}  ← no internal links pointing here")

    if broken_rows:
        print(f"\nTop broken URLs (by number of pages linking to them):")
        for r in broken_rows[:20]:
            print(f"  [{r['status']}] linked from {r['linked_from_count']:>3} page(s)  {r['url']}")
            if r["linked_from_sample"]:
                for src in r["linked_from_sample"].split(" | ")[:2]:
                    print(f"          ↳ {src}")

    if noindex_rows:
        print(f"\nNoindex pages (search engines will not index these):")
        for r in noindex_rows[:20]:
            flag = "⚠ also in sitemap" if r["in_sitemap"] else ""
            print(f"  [{r['status']}]  {r['url']}  {flag}")

    if orphan_rows:
        print(f"\nOrphan pages (nothing on the site links here):")
        for r in orphan_rows[:20]:
            sitemap_flag = "in sitemap" if r["in_sitemap"] else "NOT in sitemap ← invisible to search engines"
            print(f"  [{r['status']}]  {sitemap_flag}  {r['url']}")

    if external_rows:
        ext_4xx = [r for r in external_rows if r["status_class"] == "4xx_error"]
        ext_5xx = [r for r in external_rows if r["status_class"] == "5xx_error"]
        ext_err = [r for r in external_rows if r["status_class"] == "fetch_error"]
        print(f"\nExternal links summary ({len(external_rows):,} unique external URLs):")
        print(f"  2xx OK ........... {sum(1 for r in external_rows if r['status_class'] == '2xx_ok'):,}")
        print(f"  3xx Redirect ..... {sum(1 for r in external_rows if r['status_class'] == '3xx_redirect'):,}")
        print(f"  4xx Error ........ {len(ext_4xx):,}  ← broken external links")
        print(f"  5xx Error ........ {len(ext_5xx):,}")
        print(f"  Fetch errors ..... {len(ext_err):,}")
        if ext_4xx:
            print(f"\n  Top 4xx external URLs:")
            for r in ext_4xx[:10]:
                print(f"    [{r['status']}] linked from {r['linked_from_count']:>3} page(s)  {r['url']}")

    print(f"\nBroken URLs      → {broken_path}")
    print(f"Noindex pages    → {noindex_path}")
    print(f"Orphan pages     → {orphan_path}")
    print(f"External links   → {external_path}")
    print(f"Full crawl       → {all_path}")


if __name__ == "__main__":
    main()