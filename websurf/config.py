"""Run configuration. Caps are fail-safe defaults — raising them is the
explicit action. Budgets are hard ceilings enforced by the meter, not hints."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def default_db_path():
    """The default store is a CACHE, and lives where caches live (the OS
    user cache dir — ~/Library/Caches/websurf on macOS, ~/.cache/websurf
    on Linux). Stale pages are pruned automatically on open; deleting the file
    is always safe. Durable project output belongs in an explicit --db path."""
    try:
        from platformdirs import user_cache_dir
        base = Path(user_cache_dir("websurf"))
    except ImportError:  # stdlib fallback, same locations
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "websurf" / "Cache"
        elif os.uname().sysname == "Darwin":
            base = Path.home() / "Library" / "Caches" / "websurf"
        else:
            base = Path(os.environ.get("XDG_CACHE_HOME",
                                       Path.home() / ".cache")) / "websurf"
    return str(base / "cache.db")


@dataclass
class Config:
    db_path: str = field(default_factory=default_db_path)
    model: str = "claude-opus-4-8"
    backend: str = "auto"            # auto | api | cli | none
    mode: str = "auto"               # auto | manual

    # politeness (per-domain pacing; tunable per run via --min-delay)
    min_delay: float = 2.0
    request_timeout: int = 20
    # Honored by default; set WEBSURF_IGNORE_ROBOTS=1 (or pass --ignore-robots)
    # to bypass. robots.txt is a crawling convention — bypassing suits
    # human-directed browsing of a page or two, not bulk crawls.
    respect_robots: bool = field(default_factory=lambda: os.environ.get(
        "WEBSURF_IGNORE_ROBOTS", "").strip().lower() not in ("1", "true", "yes"))

    # fan-out caps (planning currency; engine clamps whatever the plan asks)
    cap_per_group: int = 30          # links fetched per link-group expansion
    cap_per_domain: int = 80         # NEW fetches per domain per invocation (cache hits free)
    cap_pages_run: int = 300         # pages fetched per run, all domains
    cap_paginate: int = 3            # extra pagination pages per listing
    allow_external: bool = False     # cross-domain enrichment off by default
    max_cycles: int = 4              # plan->execute cycles per run
    max_steps_per_plan: int = 6
    fast_path_pages: int = 8         # <= this many start pages => skip planning

    # budgets (enforcement currency; measured, hard stop)
    budget_tokens: int = 400_000     # LLM input+output tokens per run
    budget_fetches: int = 400
    budget_renders: int = 400        # circuit breaker only (= budget_fetches):
                                     # rendering is on-demand; fetch caps bound it
                                     # implicitly. Skips are counted + reported loudly.

    # render breaker: when a domain's renders keep classifying soft_404 (an SPA
    # serving HTTP 200 + a not-found shell for deleted pages), stop AUTO-
    # rendering that domain this invocation — the skips are counted and
    # reported loudly, and explicit --playwright still forces. Ratio-based,
    # not consecutive: live pages interleave with dead ones and would keep
    # resetting a streak counter.
    render_breaker: bool = True      # --no-render-breaker turns it off
    render_breaker_min: int = 10     # rendered pages per domain before it can trip
    render_breaker_ratio: float = 0.7  # soft_404 fraction at/above which it trips

    # cache
    max_age_days: float = 7.0        # page cache freshness window

    # projection sizes
    samples_per_group: int = 5
    singles_cap: int = 40
    group_min: int = 4               # links per dir before they form a group
    synth_html_cap: int = 45_000     # pruned-HTML chars shown for synthesis
    extract_text_cap: int = 9_000    # page text chars per LLM extraction call
    outlines_per_plan: int = 4       # representative outlines per plan call
