"""URL canonicalization and pattern grouping.

Canonical URLs back the visited-set and page cache (dedup correctness).
Group keys ("host/dir/*") are the currency the planner uses to talk about
fan-out — self-describing, stable across recomputation.
"""
import posixpath
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid",
                   "ref", "ref_src", "source", "s_kwcid", "yclid", "_hsenc",
                   "_hsmi", "hsa_acc"}
TRACKING_PREFIXES = ("utm_",)

# ToS-hostile aggregators — never crawled, ever (project etiquette).
DENYLIST = {"linkedin.com", "crunchbase.com", "pitchbook.com", "facebook.com",
            "twitter.com", "x.com", "instagram.com", "glassdoor.com"}

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEXY = re.compile(r"^[0-9a-f]{12,}$", re.I)


def canonical_domain(url_or_domain):
    s = (url_or_domain or "").strip().lower()
    if not s:
        return None
    if "://" not in s:
        s = "https://" + s
    host = urlsplit(s).netloc.split("@")[-1].split(":")[0]
    return host.removeprefix("www.") or None


def is_denied(url):
    dom = canonical_domain(url) or ""
    return any(dom == d or dom.endswith("." + d) for d in DENYLIST)


def canonicalize(url, base=None):
    """Normalized absolute URL, or None if not fetchable http(s)."""
    if not url:
        return None
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    host = parts.netloc.lower()
    if host.endswith(":80") and scheme == "http":
        host = host[:-3]
    if host.endswith(":443") and scheme == "https":
        host = host[:-4]
    if not host:
        return None
    path = posixpath.normpath(parts.path) if parts.path else "/"
    if path == ".":
        path = "/"
    if parts.path.endswith("/") and path != "/":
        path += "/"
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in TRACKING_PARAMS
             and not any(k.startswith(p) for p in TRACKING_PREFIXES)]
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def _norm_seg(seg):
    if _UUID.match(seg) or _HEXY.match(seg):
        return "{id}"
    return re.sub(r"\d+", "{n}", seg)


def _host_key(url):
    return (urlsplit(url).netloc.lower().removeprefix("www.")
            .split(":")[0] or "?")


def group_key(url):
    """'host/dir/*' pattern for links deep enough to group, else None.

    Depth-0/1 links (/, /about, /portfolio) stay individual — they're the
    nav surface the planner must see one by one.
    """
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2:
        return None
    dirpart = "/".join(_norm_seg(s) for s in segs[:-1])
    return f"{_host_key(url)}/{dirpart}/*"


def template_key(url):
    """Cluster key for template detection: group pattern, or the full
    normalized path for shallow pages."""
    gk = group_key(url)
    if gk:
        return gk
    parts = urlsplit(url)
    segs = [_norm_seg(s) for s in parts.path.split("/") if s]
    return f"{_host_key(url)}/{'/'.join(segs)}"
