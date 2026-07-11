#  Site Health Spider

A command-line web crawler that audits any site for broken links, SEO issues, and structural problems. Point it at a URL and it produces five ready-to-open CSV reports.

## What it checks

| Signal | What it means |
|---|---|
| **4xx / 5xx errors** | Pages returning HTTP errors — broken links that hurt UX and crawl budget |
| **Next.js error fallbacks** | Pages that return 200 but are silently broken (`__next_error__`) |
| **Redirect chains** | Multi-hop redirects that slow page load and dilute link equity |
| **Noindex pages** | Pages search engines are told to skip — flagged if they're also in the sitemap |
| **Orphan pages** | Pages with zero internal inlinks — invisible to crawlers unless in the sitemap |
| **External link health** | Status check on every outbound link the site contains |

## Output

Running the spider produces five CSVs named after the domain you crawled:

```
<domain>_404_report.csv       — broken URLs, sorted by how many pages link to them
<domain>_all_pages.csv        — every URL crawled, with full status metadata
<domain>_noindex.csv          — 200 pages marked noindex
<domain>_orphans.csv          — pages with no internal inlinks
<domain>_external_links.csv   — status of all outbound links
```

## Usage

```bash
pip install requests beautifulsoup4
python site_health_spider.py https://example.com
```

**Options:**

```
--workers    Concurrent threads (default: 10)
--max-urls   URL cap before stopping (default: 1500)
--timeout    Per-request timeout in seconds (default: 20)
```

**Example with options:**
```bash
python site_health_spider.py https://example.com --workers 15 --max-urls 3000
```

## How it works

1. Fetches `sitemap.xml` (and any sitemap index sub-sitemaps) to seed the crawl queue
2. Crawls all internal links discovered in `<a href>` tags, breadth-first, using a thread pool
3. For each page: records HTTP status, redirect chain length, canonical tag, noindex signals, and outbound links
4. After the internal crawl, checks every unique external URL found
5. Writes all five CSVs and prints a summary to the terminal

## Requirements

- Python 3.8+
- `requests`
- `beautifulsoup4`

## Notes

- Crawls as a browser User-Agent to get realistic responses
- Respects `#fragment` stripping and bare `?` query strings
- Treats `www.` and non-`www.` versions of a domain as the same host
- Retries on 500/502/503/504 with exponential backoff (2 attempts)
