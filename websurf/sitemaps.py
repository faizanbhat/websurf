"""Sitemap discovery: the deterministic URL source for JS-shell sites whose
listing pages carry no links in static HTML (the Headline case — an agent had
to mine sitemap-0.xml by hand; this is that move as a primitive).

robots.txt `Sitemap:` lines first, else /sitemap.xml; sitemap indexes recursed.
All fetches flow through the Fetcher — politeness, denylist, and cache apply.
Discovery only: nothing here fetches the found URLs.
"""
import re
from urllib.parse import urlsplit

LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
SITEMAP_HINT = re.compile(r"^sitemap:\s*(\S+)", re.I | re.M)


def discover(fetcher, site, max_files=20):
    """-> (page_urls, [(sitemap_file_url, health), ...]).
    `site` is a domain or URL; scheme/port are preserved if given."""
    if "://" not in site:
        site = "https://" + site
    parts = urlsplit(site)
    root = f"{parts.scheme}://{parts.netloc}"

    robots = fetcher.fetch(f"{root}/robots.txt")
    roots = SITEMAP_HINT.findall(robots.html or "") or [f"{root}/sitemap.xml"]

    urls, files, queue = [], [], list(dict.fromkeys(roots))
    while queue and len(files) < max_files:
        sm = queue.pop(0)
        page = fetcher.fetch(sm)
        files.append((sm, page.health))
        if not page.html:
            continue
        locs = LOC.findall(page.html)
        if "<sitemapindex" in page.html[:2000].lower():
            queue.extend(l for l in locs if l not in dict(files))
        else:
            urls.extend(locs)
    if queue:
        files.append((f"... {len(queue)} sitemap file(s) not read (max_files={max_files})",
                      "skipped"))
    return urls, files
