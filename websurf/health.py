"""Page health classification. Unhealthy pages never reach the planner as if
they were data — a plan built on a consent wall executes garbage.

The key distinction on JS-heavy sites: "little visible text" is NOT "no data".
A shell carrying a substantial parseable JSON payload (Next.js __NEXT_DATA__,
inline application/json) is data-COMPLETE without a render — classified
data_shell, extractable via an island spec, and never auto-rendered."""
import json
import re

BLOCKED = re.compile(
    r"just a moment|checking your browser|cf-chl|cf_chl|captcha|attention required"
    r"|access denied|are you a robot|verify you are human|request unsuccessful"
    r"|enable javascript and cookies to continue|ddos protection", re.I)
SOFT404 = re.compile(
    r"page not found|404\b|doesn.t exist|no longer available|nothing (was )?found", re.I)
COOKIE = re.compile(
    r"(accept|allow|manage)( all)? cookies|cookie (policy|consent|settings)"
    r"|we (use|value your) cookies", re.I)
SPA_ROOT = re.compile(r'id=["\'](root|app|__next|___gatsby)["\']|data-reactroot|ng-app', re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_ISLAND = re.compile(
    r'<script[^>]*(?:id=["\']__NEXT_DATA__["\']|type=["\'](?:application|text)/json["\'])'
    r"[^>]*>(.*?)</script>", re.I | re.S)


def has_data_island(html, min_chars=2000):
    """Substantial parseable JSON embedded in the page — the mark of a shell
    that is data-complete without rendering. Non-parseable state blobs
    (window.__NUXT__=function...) do NOT count: they aren't extractable."""
    for m in _ISLAND.finditer(html or ""):
        blob = m.group(1).strip()
        if len(blob) < min_chars:
            continue
        try:
            json.loads(blob)
            return True
        except json.JSONDecodeError:
            continue
    return False


def classify(status, html, text):
    """-> ok | blocked | http_error | soft_404 | wall | data_shell | js_shell | empty | error"""
    if not html:
        return "error"
    head = html[:6000]
    if status in (401, 403, 407, 429, 451) or BLOCKED.search(head):
        return "blocked"
    if status and status >= 400:
        return "http_error"
    if not text:
        if has_data_island(html):
            return "data_shell"
        return "js_shell" if SPA_ROOT.search(head) else "empty"
    m = _TITLE.search(head)
    title = m.group(1) if m else ""
    if SOFT404.search(title) or (len(text) < 400 and SOFT404.search(text[:200])):
        return "soft_404"
    if len(text) < 600 and (SPA_ROOT.search(head) or len(html) > 20 * max(len(text), 1)):
        return "data_shell" if has_data_island(html) else "js_shell"
    if len(text) < 800 and COOKIE.search(text):
        return "wall"
    return "ok"


# data_shell is deliberately absent: rendering a data-complete shell is waste.
RETRY_WITH_BROWSER = {"js_shell", "blocked", "empty"}
