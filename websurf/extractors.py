"""Declarative extractors: the LLM writes them once per template; this module
applies them deterministically and health-checks the output.

Spec format (JSON, produced by synthesis):
  {
    "kind": "list" | "detail",
    "item_selector": "div.portfolio-card",     # list only; repeating node
    "fields": {
      "name":        {"sel": "h3",        "attr": "text"},
      "url":         {"sel": "a",         "attr": "href"},
      "description": {"sel": "p.blurb",   "attr": "text"}
    }
  }
`sel` is a CSS selector relative to the item (empty string = the item itself).
`attr` is text | html | href | src | <any attribute name>. href/src absolutized.

Island specs read embedded JSON (Next.js __NEXT_DATA__ etc.) instead of the DOM —
the data source on JS-shell pages, applied with zero renders and zero tokens:
  {
    "kind": "island",
    "island_selector": "script#__NEXT_DATA__",   # CSS selector of the JSON <script>
    "path": "props.pageProps.companies",         # dotted path to the entity array
    "fields": {"name": "title", "url": "website.url"}   # field -> path within one item
  }
Dotted paths may use numeric segments for list indices; a non-numeric segment on a
list descends into its first element. Root-relative values ("/portfolio/x") absolutized.

Also here: the zero-token structured-data mapper (JSON-LD schema.org -> fields).
"""
import json
import math
from urllib.parse import urljoin

from bs4 import BeautifulSoup


class SpecError(ValueError):
    pass


def validate_spec(spec, sample_html):
    """Raise SpecError unless spec is well-formed and matches the sample."""
    if not isinstance(spec, dict) or not isinstance(spec.get("fields"), dict):
        raise SpecError("spec must be a dict with a 'fields' dict")
    if spec.get("kind") not in ("list", "detail", "island"):
        raise SpecError("spec.kind must be 'list', 'detail' or 'island'")
    if spec["kind"] == "island":
        if not spec.get("island_selector"):
            raise SpecError("island spec needs island_selector")
        if not all(isinstance(p, str) for p in spec["fields"].values()):
            raise SpecError("island spec fields must map field -> dotted JSON path")
        if not _island_items(spec, sample_html):
            raise SpecError(
                f"island path {spec.get('path')!r} resolves to nothing in the sample")
        return
    if spec["kind"] == "list" and not spec.get("item_selector"):
        raise SpecError("list spec needs item_selector")
    soup = BeautifulSoup(sample_html or "", "lxml")
    try:
        if spec["kind"] == "list":
            items = soup.select(spec["item_selector"])
            if not items:
                raise SpecError(f"item_selector {spec['item_selector']!r} matches nothing")
        for name, f in spec["fields"].items():
            if not isinstance(f, dict) or "attr" not in f:
                raise SpecError(f"field {name!r} needs {{sel, attr}}")
            if f.get("sel"):
                soup.select(f["sel"])  # compiles or raises
    except SpecError:
        raise
    except Exception as e:
        raise SpecError(f"selector error: {e}") from e


def _field_value(root, f, base_url):
    el = root.select_one(f["sel"]) if f.get("sel") else root
    if el is None:
        return None
    attr = f.get("attr", "text")
    if attr == "text":
        val = el.get_text(" ", strip=True)
    elif attr == "html":
        val = str(el)
    else:
        val = el.get(attr)
        if val and attr in ("href", "src"):
            val = urljoin(base_url, val)
    return val or None


def _json_get(node, path):
    cur = node
    for part in (path.split(".") if path else []):
        if isinstance(cur, list):
            if part.isdigit():
                cur = cur[int(part)] if int(part) < len(cur) else None
                continue
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _island_items(spec, html):
    """The entity dicts an island spec's path resolves to (first matching
    script whose JSON parses and whose path lands on data)."""
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup.select(spec["island_selector"]):
        try:
            data = json.loads((tag.string or tag.get_text() or "").strip())
        except (json.JSONDecodeError, ValueError):
            continue
        node = _json_get(data, spec.get("path") or "")
        if isinstance(node, list):
            items = [x for x in node if isinstance(x, dict)]
            if items:
                return items
        elif isinstance(node, dict):
            return [node]
    return []


def _island_value(item, path, base_url):
    val = _json_get(item, path)
    if isinstance(val, list):
        val = next((v for v in val if isinstance(v, (str, int, float))), None)
    if isinstance(val, dict):
        return None  # path stops short of a value — spec should go deeper
    if isinstance(val, str):
        val = val.strip()
        if val.startswith("/") and " " not in val:  # root-relative URL
            val = urljoin(base_url, val)
    return val if val not in ("", None) else None


def apply_spec(spec, html, base_url):
    """-> list of {field: value} records (one per item; one for detail pages)."""
    if spec["kind"] == "island":
        out = []
        for item in _island_items(spec, html):
            rec = {name: _island_value(item, p, base_url)
                   for name, p in spec["fields"].items()}
            if any(v for v in rec.values()):
                out.append(rec)
        return out
    soup = BeautifulSoup(html or "", "lxml")
    roots = soup.select(spec["item_selector"]) if spec["kind"] == "list" else [soup]
    out = []
    for root in roots:
        rec = {name: _field_value(root, f, base_url) for name, f in spec["fields"].items()}
        if any(v for v in rec.values()):
            out.append(rec)
    return out


def check_health(records, fields, expected_count=None):
    """Deterministic gate on extractor output — this is what makes 'repair'
    a defined event instead of a vibe. -> (ok, [reasons])."""
    reasons = []
    if not records:
        return False, ["zero records"]
    key = fields[0] if fields else next(iter(records[0]), None)
    if key:
        vals = [r.get(key) for r in records]
        null_rate = sum(1 for v in vals if not v) / len(vals)
        if null_rate > 0.5:
            reasons.append(f"key field {key!r} null in {null_rate:.0%} of records")
        nonnull = [v for v in vals if v]
        if len(nonnull) >= 4 and len(set(nonnull)) / len(nonnull) < 0.5:
            reasons.append(f"key field {key!r} collapsed to few distinct values")
    for f in fields[1:]:
        vals = [r.get(f) for r in records]
        if vals and sum(1 for v in vals if not v) / len(vals) > 0.8:
            reasons.append(f"field {f!r} null in >80% of records")
    if expected_count and len(records) < expected_count * 0.5:
        reasons.append(f"got {len(records)} records, expected ~{expected_count}")
    return not reasons, reasons


# -- structured-data mapper (zero LLM tokens) ---------------------------------

SD_ALIASES = {
    "name": ["name", "headline", "title"],
    "title": ["name", "headline", "title"],
    "description": ["description"],
    "price": ["offers.price", "offers.lowPrice", "price"],
    "currency": ["offers.priceCurrency", "priceCurrency"],
    "availability": ["offers.availability", "availability"],
    "image": ["image"],
    "brand": ["brand.name", "brand"],
    "sku": ["sku"],
    "url": ["url", "@id"],
    "rating": ["aggregateRating.ratingValue"],
    "date": ["datePublished", "startDate"],
    "author": ["author.name", "author"],
}


# Node types that describe the SITE rather than a content entity — matching
# them would return the publisher (e.g. the VC firm itself) as a record.
SD_SKIP_TYPES = {"Organization", "WebSite", "WebPage", "BreadcrumbList",
                 "SiteNavigationElement", "SearchAction", "ImageObject",
                 "CollectionPage", "ItemList"}


def _sd_nodes(blocks):
    nodes = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        nodes.append(b)
        graph = b.get("@graph")
        if isinstance(graph, list):
            nodes.extend(n for n in graph if isinstance(n, dict))
    def keep(n):
        t = n.get("@type")
        types = set(t) if isinstance(t, list) else {t}
        return not (types & SD_SKIP_TYPES)
    return [n for n in nodes if keep(n)]


def _sd_get(node, path):
    cur = node
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    if isinstance(cur, list):
        cur = cur[0] if cur else None
    if isinstance(cur, dict):
        cur = cur.get("name") or cur.get("@id")
    return str(cur) if cur is not None else None


def sd_extract(blocks, fields):
    """Map requested fields onto the best JSON-LD node. Returns a record if
    at least half the fields resolve, else None."""
    best, best_hits = None, 0
    for node in _sd_nodes(blocks):
        rec = {}
        for field in fields:
            for path in SD_ALIASES.get(field.lower(), [field]):
                val = _sd_get(node, path)
                if val:
                    rec[field] = val
                    break
        hits = sum(1 for f in fields if rec.get(f))
        if hits > best_hits:
            best, best_hits = rec, hits
    if best and best_hits >= max(1, math.ceil(len(fields) / 2)):
        return {f: best.get(f) for f in fields}
    return None
