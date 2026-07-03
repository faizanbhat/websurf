"""Fetch layer (lifted from v1 and extended): requests fast path, Playwright
fallback on unhealthy responses, per-domain throttling, robots.txt, denylist,
content-addressed page cache, and meter enforcement.

Boring and load-bearing. Every guardrail here is enforced in code, not prompt.
"""
import atexit
import sys
import threading
import time
import urllib.robotparser
from dataclasses import dataclass

import requests

from . import health as health_mod
from . import urls as urls_mod
from .projections import main_text

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class Page:
    url: str                      # canonical
    final_url: str = ""
    status: int | None = None
    html: str = ""
    method: str = "requests"
    health: str = "error"
    error: str | None = None
    from_cache: bool = False


class Fetcher:
    def __init__(self, config, store, meter):
        self.config = config
        self.store = store
        self.meter = meter
        self._slots = {}
        self._lock = threading.Lock()
        self._robots = {}
        self._tl = threading.local()
        self._domain_fetched = {}   # network fetches this invocation (cache hits free)
        self._render_stats = {}     # domain -> (renders, soft_404s) this invocation
        self.domain_capped = {}     # domain -> URLs refused by the per-domain ceiling
        self.render_skipped = {}    # domain -> shells left un-rendered (render budget)
        self.render_broken = {}     # domain -> shells left un-rendered (render breaker)

    # -- politeness -----------------------------------------------------------

    def _throttle(self, domain):
        delay = self.config.min_delay
        rp = self._robots.get(domain)
        if rp:
            try:
                delay = max(delay, rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*") or 0)
            except Exception:
                pass
        with self._lock:
            now = time.monotonic()
            start = max(now, self._slots.get(domain, 0.0))
            self._slots[domain] = start + delay
        wait = start - now
        if wait > 0:
            time.sleep(wait)

    def _robots_allowed(self, url, domain):
        if not self.config.respect_robots:
            return True
        if domain not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                from urllib.parse import urlsplit
                parts = urlsplit(url)
                resp = requests.get(f"{parts.scheme}://{parts.netloc}/robots.txt",
                                    headers=HEADERS, timeout=self.config.request_timeout)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp = None  # no robots -> allow
            except requests.RequestException:
                rp = None
            self._robots[domain] = rp
        rp = self._robots[domain]
        if rp is None:
            return True
        try:
            return rp.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    # -- transports -----------------------------------------------------------

    def _fetch_requests(self, url):
        p = Page(url=url)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=self.config.request_timeout,
                                allow_redirects=True)
            p.status = resp.status_code
            p.final_url = str(resp.url)
            ctype = resp.headers.get("content-type", "")
            if not ctype or "html" in ctype or "xml" in ctype or "text/plain" in ctype:
                p.html = resp.text  # plain text kept for robots.txt / sitemap discovery
        except requests.RequestException as e:
            p.error = f"{type(e).__name__}: {e}"
        return p

    def _browser(self):
        if getattr(self._tl, "browser", None) is None:
            from playwright.sync_api import sync_playwright
            self._tl.pw = sync_playwright().start()
            self._tl.browser = self._tl.pw.chromium.launch(headless=True)
            # Deterministic teardown: without it the interpreter exits while a
            # greenlet still references the driver transport — SIGSEGV in
            # greenlet g_switch during finalization (one macOS CrashReporter
            # modal per rendering invocation). atexit runs pre-finalization.
            atexit.register(self.close)
        return self._tl.browser

    def close(self):
        """Close the browser and stop Playwright (idempotent, exception-safe)."""
        for attr, method in (("browser", "close"), ("pw", "stop")):
            obj = getattr(self._tl, attr, None)
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
                setattr(self._tl, attr, None)

    def _fetch_playwright(self, url):
        p = Page(url=url, method="playwright")
        try:
            ctx = self._browser().new_context(
                user_agent=USER_AGENT, viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=35000)
                p.status = resp.status if resp else None
                page.wait_for_timeout(1500)
                for _ in range(5):  # nudge lazy-loaded grids
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(400)
                p.html = page.content()
                p.final_url = page.url
            finally:
                ctx.close()
        except Exception as e:
            p.error = f"{type(e).__name__}: {e}"
        return p

    # -- render breaker ---------------------------------------------------------
    # SPAs commonly serve HTTP 200 + a not-found shell for deleted entities, so
    # a sweep over stale sitemap URLs looks healthy until AFTER each render.
    # Once a domain's renders are measurably waste, stop AUTO-rendering it this
    # invocation — loudly. Ratio-based, not consecutive: live pages interleave
    # with dead ones and would keep resetting a streak counter.

    def _note_render(self, domain, health):
        n, soft = self._render_stats.get(domain, (0, 0))
        self._render_stats[domain] = (n + 1, soft + (health == "soft_404"))

    def _breaker_ok(self, domain):
        if not self.config.render_breaker:
            return True
        n, soft = self._render_stats.get(domain, (0, 0))
        return (n < self.config.render_breaker_min
                or soft / n < self.config.render_breaker_ratio)

    def _count_broken(self, domain):
        if domain not in self.render_broken:  # trip note once, AT trip time —
            # a batch should say why it sped up, not leave it to the summary
            n, soft = self._render_stats.get(domain, (0, 0))
            print(f"# render breaker tripped on {domain}: {soft} of {n} rendered "
                  f"pages classified soft_404 — leaving further shells "
                  f"un-rendered (--no-render-breaker forces)", file=sys.stderr)
        self.render_broken[domain] = self.render_broken.get(domain, 0) + 1

    # -- public ---------------------------------------------------------------

    def fetch(self, url, force_refresh=False, force_playwright=False,
              allow_render=True):
        """Canonicalize, consult cache, respect denylist/robots/ceilings,
        fetch (with browser fallback), classify health, persist.

        allow_render=False: never escalate to the browser — no cache-heal, no
        shell fallback. For callers that consume the un-rendered HTML itself
        (extract: island and meta specs read the shell; a spec that needs
        rendered DOM gets it via an explicit `fetch --playwright` first)."""
        canon = urls_mod.canonicalize(url)
        if not canon:
            return Page(url=url, health="error", error="not a fetchable http(s) URL")
        domain = urls_mod.canonical_domain(canon)

        if urls_mod.is_denied(canon):
            return Page(url=canon, health="denied", error="denylisted domain")

        auto_render = False  # heal/fallback renders — the breaker gates these,
        #                      never an operator's explicit force_playwright
        if not force_refresh:
            row = self.store.get_page(canon, self.config.max_age_days)
            if row:
                # Cache-heal: a shell cached UN-RENDERED (render budget was
                # exhausted, or rendering failed) must not be served as data
                # for a week — retry it with the browser instead. Loudly:
                # upgrading a cache read into a multi-second render must never
                # look like a hang.
                heal = (allow_render and not force_playwright
                        and row["health"] in health_mod.RETRY_WITH_BROWSER
                        and row["method"] == "requests"
                        and self.meter.renders_available())
                if heal and not self._breaker_ok(domain):
                    self._count_broken(domain)
                    heal = False
                if heal:
                    print(f"# cache-heal: {canon} cached un-rendered "
                          f"({row['health']}) — re-fetching with browser",
                          file=sys.stderr)
                    auto_render = True
                else:
                    self.meter.cache_hits += 1
                    return Page(url=canon, final_url=row["final_url"], status=row["status"],
                                html=self.store.page_html(canon) or "", method=row["method"],
                                health=row["health"], from_cache=True)

        # Per-domain ceiling counts NEW network fetches this invocation only —
        # cache hits are free and past runs don't eat the budget.
        if self._domain_fetched.get(domain, 0) >= self.config.cap_per_domain:
            self.domain_capped[domain] = self.domain_capped.get(domain, 0) + 1
            return Page(url=canon, health="domain_capped",
                        error=f"per-domain ceiling {self.config.cap_per_domain} reached "
                              f"— raise with --cap-per-domain")
        if not self._robots_allowed(canon, domain):
            return Page(url=canon, health="robots_disallowed", error="robots.txt disallows")

        self.meter.count_fetch()
        self._domain_fetched[domain] = self._domain_fetched.get(domain, 0) + 1
        result = None
        if not (force_playwright or auto_render):
            self._throttle(domain)
            result = self._fetch_requests(canon)
            result.health = health_mod.classify(
                result.status, result.html, main_text(result.html))
        wants_render = force_playwright or auto_render or (
            allow_render and result is not None
            and result.health in health_mod.RETRY_WITH_BROWSER)
        if wants_render:
            if (result is not None and not force_playwright
                    and not self._breaker_ok(domain)):
                self._count_broken(domain)  # shell kept as-is, counted loudly
            elif self.meter.renders_available() or force_playwright:
                self.meter.count_render()  # raises loudly if forced past the budget
                self._throttle(domain)
                pw = self._fetch_playwright(canon)
                pw.health = health_mod.classify(pw.status, pw.html, main_text(pw.html))
                self._note_render(domain, pw.health)
                if result is None or _better(pw, result):
                    result = pw
            else:
                # Never a silent degrade: the shell is kept and cached, but the
                # skip is counted and summarized (and cache-heal retries it on
                # the next invocation with budget).
                self.render_skipped[domain] = self.render_skipped.get(domain, 0) + 1
        result.url = canon
        self.store.save_page(canon, result.final_url or canon, domain, result.status,
                             result.method, result.health, result.html)
        return result

    def cap_summary(self):
        """Loud, end-of-batch truncation lines. Empty list = nothing was capped."""
        lines = [f"{n} URL(s) hit cap_per_domain={self.config.cap_per_domain} on {d} "
                 f"— data is INCOMPLETE; re-run with --cap-per-domain to fetch the rest"
                 for d, n in sorted(self.domain_capped.items())]
        lines += [f"{n} shell page(s) on {d} left UN-RENDERED (render budget "
                  f"{self.config.budget_renders}) — data is INCOMPLETE; re-run with "
                  f"--budget-renders raised (cached shells are retried automatically)"
                  for d, n in sorted(self.render_skipped.items())]
        lines += [f"render breaker on {d}: {self._render_stats[d][1]} of "
                  f"{self._render_stats[d][0]} rendered pages classified soft_404 — "
                  f"{n} further shell(s) left UN-RENDERED. These URLs likely point "
                  f"to deleted pages (an SPA serving HTTP 200 + a not-found shell); "
                  f"spot-check with `probe URL --playwright` or re-run with "
                  f"--no-render-breaker"
                  for d, n in sorted(self.render_broken.items())]
        return lines


def _better(a, b):
    rank = {"ok": 0, "data_shell": 1, "wall": 2, "soft_404": 3, "js_shell": 4,
            "empty": 5, "blocked": 6, "http_error": 7, "error": 8}
    ra, rb = rank.get(a.health, 9), rank.get(b.health, 9)
    if ra != rb:
        return ra < rb
    return len(a.html or "") >= len(b.html or "")
