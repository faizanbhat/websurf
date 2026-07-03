"""Projections: deterministic, cheap views of a fetched page.

- outline: the probe artifact — title/meta/headings/structured-data + links
  grouped by DOM region and URL pattern WITH COUNTS. The planner sees the
  shape of 2000 links without seeing all of them; the expand channel exists
  so truncation is never a dead end.
- main_text: readability-ish body text.
- prune_html: slimmed HTML sample for extractor synthesis (keeps structure,
  class/id/href; drops scripts, styles, long attrs).
- dom_shape / structured_data / next_page helpers.
"""
import hashlib
import json
import re
from collections import Counter

from bs4 import BeautifulSoup

from . import urls as urls_mod

STRIP_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "canvas"]
CHROME_TAGS = ["nav", "header", "footer", "aside"]
NEXT_WORDS = re.compile(r"^(next|more|older|›|»|→|load more|show more)\b", re.I)


def _soup(html):
    return BeautifulSoup(html or "", "lxml")


def main_text(html, cap=60_000):
    soup = _soup(html)
    for tag in soup(STRIP_TAGS + CHROME_TAGS):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.find(role="main")
    if root is None or len(root.get_text(strip=True)) < 200:
        root = soup.body or soup
    text = root.get_text("\n", strip=True)
    return re.sub(r"\n{2,}", "\n", text)[:cap]


def structured_data(html):
    """Parsed JSON-LD blocks plus OpenGraph tags."""
    soup = _soup(html)
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        blocks.extend(data if isinstance(data, list) else [data])
    og = {}
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        if prop.startswith("og:") and meta.get("content"):
            og[prop] = meta["content"].strip()
    return blocks, og


ISLAND_SELECTOR = ('script[id="__NEXT_DATA__"], script[type="application/json"], '
                   'script[type="text/json"]')


def _json_arrays(obj, path="", depth=0, out=None):
    """Arrays-of-objects inside a parsed JSON blob — where the records live."""
    if out is None:
        out = []
    if depth > 8:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _json_arrays(v, f"{path}.{k}" if path else k, depth + 1, out)
    elif isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if len(dicts) >= 3:
            keys = sorted(set().union(*(d.keys() for d in dicts[:5])))
            out.append({"path": path, "count": len(obj), "item_keys": keys[:20],
                        "sample_item": json.dumps(dicts[0], default=str)[:600]})
        for i, x in enumerate(obj[:3]):
            _json_arrays(x, f"{path}.{i}", depth + 1, out)
    return out


def data_islands(html, min_chars=2000, top=3):
    """Embedded JSON payloads (Next.js __NEXT_DATA__, inline application/json):
    the cheapest data source on JS-shell pages — the full dataset is usually
    right here, no render needed. Summarized as selector + the largest
    arrays-of-objects (path, count, item keys, one sample item) so an island
    extractor spec can be written from the outline alone."""
    soup = _soup(html)
    out = []
    for tag in soup.select(ISLAND_SELECTOR):
        blob = (tag.string or tag.get_text() or "").strip()
        if len(blob) < min_chars:
            continue
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        arrays = sorted(_json_arrays(parsed), key=lambda a: -a["count"])
        sel = (f"script#{tag.get('id')}" if tag.get("id")
               else f"script[type=\"{tag.get('type')}\"]")
        out.append({"selector": sel, "chars": len(blob), "arrays": arrays[:top]})
    out.sort(key=lambda i: -(i["arrays"][0]["count"] if i["arrays"] else 0))
    return out or None


def dom_shape(html):
    """Crude template fingerprint: top (tag, first-class) pairs by frequency."""
    soup = _soup(html)
    counts = Counter()
    for tag in soup.find_all(True):
        cls = (tag.get("class") or [""])[0]
        counts[(tag.name, cls)] += 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    return hashlib.sha1(repr(top).encode()).hexdigest()[:10]


def _region(a):
    node = a
    while node is not None and node.name != "[document]":
        if node.name in ("nav", "header", "footer", "aside", "main", "article"):
            return node.name
        role = node.get("role") if hasattr(node, "get") else None
        if role in ("navigation", "main", "contentinfo", "banner"):
            return {"navigation": "nav", "contentinfo": "footer", "banner": "header"}.get(role, role)
        node = node.parent
    return "body"


def page_links(html, base_url):
    """All canonicalized links: [{url, text, region}], deduped by URL."""
    soup = _soup(html)
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    seen, out = {}, []
    for a in soup.find_all("a", href=True):
        url = urls_mod.canonicalize(a["href"], base=base_url)
        if not url:
            continue
        text = a.get_text(" ", strip=True)
        if not text:
            img = a.find("img")
            text = (img.get("alt", "").strip() if img else "") or "(image)"
        if url in seen:
            if seen[url]["text"] == "(image)" and text != "(image)":
                seen[url]["text"] = text
            continue
        entry = {"url": url, "text": text[:120], "region": _region(a)}
        seen[url] = entry
        out.append(entry)
    return out


def group_links(links, config, page_host=None):
    """Split links into pattern groups (count >= group_min), shown singles,
    an omitted-singles summary, and a denied summary.

    Returns (groups, singles, omitted, denied). Group ids ARE the patterns —
    self-describing and stable across recomputation. Denylisted links never
    become an affordance: reported as counts, not offered as groups.
    """
    denied = {}
    by_key = {}
    for link in links:
        if urls_mod.is_denied(link["url"]):
            key = (urls_mod.group_key(link["url"])
                   or urls_mod.canonical_domain(link["url"]) or "?")
            denied[key] = denied.get(key, 0) + 1
            continue
        key = urls_mod.group_key(link["url"])
        by_key.setdefault(key, []).append(link)

    groups, singles = [], []
    for key, members in by_key.items():
        if key is not None and len(members) >= config.group_min:
            region = Counter(m["region"] for m in members).most_common(1)[0][0]
            host = key.split("/", 1)[0]
            groups.append({
                "id": key, "region": region, "count": len(members),
                "external": bool(page_host and host != page_host),
                "samples": [[m["text"], m["url"]] for m in members[:config.samples_per_group]],
            })
        else:
            singles.extend(members)
    # Content groups first, nav/footer chrome after, external last — order is
    # a nudge; nothing is hidden.
    content_rank = {"main": 0, "article": 0, "body": 0, "aside": 1,
                    "header": 2, "nav": 2, "footer": 3}
    groups.sort(key=lambda g: (g["external"],
                               content_rank.get(g["region"], 1), -g["count"]))

    region_rank = {"nav": 0, "main": 1, "body": 2, "article": 2, "header": 3,
                   "aside": 4, "footer": 5}
    singles.sort(key=lambda s: region_rank.get(s["region"], 3))
    shown = singles[:config.singles_cap]
    omitted = None
    if len(singles) > config.singles_cap:
        tail = singles[config.singles_cap:]
        omitted = {
            "count": len(tail),
            "by_region": dict(Counter(t["region"] for t in tail)),
            "note": ("outline display cap only — nothing is lost: extraction "
                     "reads full page HTML; list every link via: expand <url> '*'"),
        }
    return groups, shown, omitted, (denied or None)


def expand_group(html, base_url, group_id):
    """All links on the page whose pattern matches group_id; '*' lists every
    link (the expand channel — outline truncation is never a dead end)."""
    links = page_links(html, base_url)
    if group_id == "*":
        return links
    return [l for l in links if urls_mod.group_key(l["url"]) == group_id]


REPEAT_TAGS = ["div", "article", "section", "li", "tr"]
_CSS_CLASS = re.compile(r"^[a-zA-Z_][-\w]*$")


def repeats(html, base_url, group_ids, top=3, min_count=4):
    """Repeating content blocks: deterministic evidence for the single most
    cost-determining decision — is the wanted data already ON this page, or
    only linked from it? Reports count, text volume, one sample's text, and
    which link group's members live inside the blocks. Facts, not verdicts.
    """
    soup = _soup(html)
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    buckets = {}
    for el in soup.find_all(REPEAT_TAGS):
        cls = (el.get("class") or [None])[0]
        if not cls or not _CSS_CLASS.match(cls):
            continue
        buckets.setdefault((el.name, cls), []).append(el)

    out = []
    for (name, cls), els in buckets.items():
        if len(els) < min_count:
            continue
        sample = els[:20]
        avg = sum(len(e.get_text(" ", strip=True)) for e in sample) // len(sample)
        contained = Counter()
        for e in sample:
            for a in e.find_all("a", href=True):
                gk = urls_mod.group_key(
                    urls_mod.canonicalize(a["href"], base=base_url) or "")
                if gk:
                    contained[gk] += 1
        contains = next((g for g, n in contained.most_common()
                         if g in group_ids and n >= 3), None)
        out.append({"selector": f"{name}.{cls}", "count": len(els),
                    "avg_text_chars": avg,
                    "sample_text": sample[0].get_text(" ", strip=True)[:200],
                    "contains_group": contains})
    out.sort(key=lambda r: -(r["count"] * max(r["avg_text_chars"], 1)))
    return out[:top]


def microdata_summary(soup):
    """Schema.org microdata is detected, not parsed: extraction of it flows
    through synthesis ([itemprop=...] selectors), which is health-checked —
    a hand-rolled microdata parser feeding the zero-token path would fail
    silently on nested scopes."""
    scopes = soup.find_all(attrs={"itemscope": True})
    props = sorted({str(p.get("itemprop"))
                    for p in soup.find_all(attrs={"itemprop": True})})
    if not scopes and not props:
        return None
    types = sorted({re.sub(r"^https?://", "", s.get("itemtype", "")).rstrip("/")
                    for s in scopes if s.get("itemtype")})
    return {"scopes": len(scopes), "types": types[:10], "props": props[:30]}


def next_page(html, base_url):
    """Pagination hint: rel=next, 'next'-ish anchors, or ?page=N+1 links."""
    soup = _soup(html)
    link = soup.find("link", rel=lambda v: v and "next" in v)
    if link and link.get("href"):
        return urls_mod.canonicalize(link["href"], base=base_url)
    for a in soup.find_all("a", href=True):
        rel = a.get("rel") or []
        if "next" in rel or NEXT_WORDS.match(a.get_text(" ", strip=True) or ""):
            return urls_mod.canonicalize(a["href"], base=base_url)
    return None


def outline(page, config):
    """The probe artifact handed to the planner. `page` is a fetch.Page."""
    html, base = page.html, page.final_url or page.url
    soup = _soup(html)
    title = soup.title.get_text(strip=True) if soup.title else ""
    desc = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"})
    headings = [f"{h.name}: {h.get_text(' ', strip=True)[:120]}"
                for h in soup.find_all(["h1", "h2", "h3"], limit=15)]
    sd, og = structured_data(html)
    text = main_text(html)
    links = page_links(html, base)
    groups, singles, omitted, denied = group_links(
        links, config, urls_mod.canonical_domain(base))
    return {
        "url": page.url,
        "status": page.status,
        "health": page.health,
        "title": title[:200],
        "meta_description": (desc.get("content", "").strip()[:300] if desc else ""),
        "headings": headings,
        "structured_data_types": sorted({str(b.get("@type")) for b in sd if isinstance(b, dict)}),
        "structured_data": json.dumps(sd, default=str)[:2000] if sd else None,
        "microdata": microdata_summary(soup),
        "data_islands": data_islands(html),
        "og": og or None,
        "text_chars": len(text),
        "text_preview": text[:400],
        "repeats": repeats(html, base, [g["id"] for g in groups]),
        "links": {"total": len(links), "groups": groups, "singles": singles,
                  "singles_omitted": omitted, "denied": denied},
        "next_page": next_page(html, base),
    }


KEEP_ATTRS = {"class", "id", "href", "src", "alt", "title", "datetime", "content",
              "property", "name", "type", "rel",
              "itemprop", "itemscope", "itemtype"}


def prune_html(html, cap=45_000):
    """Slim HTML for extractor synthesis: structure + selector-relevant attrs."""
    soup = _soup(html)
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    for c in soup.find_all(string=lambda s: s.__class__.__name__ == "Comment"):
        c.extract()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr not in KEEP_ATTRS:
                del tag.attrs[attr]
            elif attr == "src" and str(tag.attrs[attr]).startswith("data:"):
                tag.attrs[attr] = "data:..."
    out = str(soup.body or soup)
    out = re.sub(r"\n\s*\n", "\n", out)
    return out[:cap]
