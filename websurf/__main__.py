#!/usr/bin/env python3
"""websurf — the web instrument for agents: browse, probe, and extract.

AI AGENTS: run `websurf skill` FIRST — the full driving guide (cost ladder,
JS-site handling, storage rules). `websurf skill --install` persists it so
future Claude Code sessions discover this tool automatically.

The LLM plans navigation and synthesizes extractors; deterministic code
fetches, applies, meters, and clamps. Two ways to drive it:
  primitives   probe / expand / fetch / text / synthesize / extract — the
               deterministic building blocks, callable one at a time (this is
               how Claude drives it in-session without an API key).
  autonomous   websurf run "objective" URL... --fields a,b,c
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from websurf.config import Config, default_db_path
from websurf.engine import Engine
from websurf.fetch import Fetcher
from websurf.llm import LLM, Meter
from websurf.store import Store
from websurf import extractors as ex
from websurf import planner
from websurf import projections as proj


def build(args, need_llm=False, readonly=False):
    cfg = Config(db_path=args.db)
    for attr in ("model", "backend", "mode"):
        if getattr(args, attr, None):
            setattr(cfg, attr, getattr(args, attr))
    for flag, attr in [("cap_per_group", "cap_per_group"), ("cap_pages", "cap_pages_run"),
                       ("cap_per_domain", "cap_per_domain"),
                       ("budget_tokens", "budget_tokens"),
                       ("budget_renders", "budget_renders"), ("max_age", "max_age_days"),
                       ("min_delay", "min_delay")]:
        if getattr(args, flag, None) is not None:
            setattr(cfg, attr, getattr(args, flag))
    if getattr(args, "allow_external", False):
        cfg.allow_external = True
    if getattr(args, "no_render_breaker", False):
        cfg.render_breaker = False
    is_file_db = cfg.db_path != ":memory:"
    if is_file_db and readonly and not Path(cfg.db_path).exists():
        # read-only commands never materialize a store
        raise SystemExit(f"no store at {cfg.db_path} — nothing crawled here yet")
    if is_file_db:
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    if not readonly and is_file_db and cfg.db_path == default_db_path():
        # the default store is a CACHE: keep it bounded. Explicit --db stores
        # are the user's — never auto-delete from them.
        pruned = store.prune_pages(cfg.max_age_days)
        if pruned:
            print(f"# cache: pruned {pruned} pages older than "
                  f"{cfg.max_age_days:g}d", file=sys.stderr)
    meter = Meter(cfg)
    fetcher = Fetcher(cfg, store, meter)
    llm = LLM(cfg, meter) if need_llm else None
    return cfg, store, meter, fetcher, llm


def cmd_run(args):
    cfg, store, meter, fetcher, llm = build(args, need_llm=True)
    fields = [f.strip() for f in (args.fields or "").split(",") if f.strip()]
    engine = Engine(cfg, store, fetcher, llm)
    report = engine.run(args.objective, args.urls, fields)
    print()
    _print_report(report)
    if args.json:
        print(json.dumps(report, indent=2, default=str))


def _print_report(r):
    print(f"run {r['run_id']}: {r['state']} — {r['records']} records "
          f"({r['records_by_extractor']})")
    print(f"pages: {r['pages_by_health']}  "
          f"llm: {r['meter']['llm_calls']} calls, "
          f"{r['meter']['tokens_in'] + r['meter']['tokens_out']} tokens  "
          f"fetches: {r['meter']['fetches']} (+{r['meter']['cache_hits']} cached)")
    for c in r["clamps"]:
        print(f"  clamp: {c}")
    for n in r["notes"]:
        print(f"  note: {n}")
    print(f"export: python websurf/__main__.py export --run {r['run_id']}")


def cmd_probe(args):
    cfg, store, meter, fetcher, _ = build(args)
    page = fetcher.fetch(args.url, force_playwright=args.playwright)
    print(json.dumps(proj.outline(page, cfg), indent=2, default=str))


def cmd_expand(args):
    cfg, store, meter, fetcher, _ = build(args)
    page = fetcher.fetch(args.url)
    links = proj.expand_group(page.html, page.final_url or page.url, args.group)
    for l in links:
        print(f"{l['url']}\t{l['text']}")
    print(f"# {len(links)} links in group {args.group!r}", file=sys.stderr)


def cmd_fetch(args):
    cfg, store, meter, fetcher, _ = build(args)
    from collections import Counter
    from websurf import urls as urls_mod
    per_domain = Counter(d for d in (urls_mod.canonical_domain(
        urls_mod.canonicalize(u) or "") for u in args.urls) if d)
    for d, n in sorted(per_domain.items()):
        if n > cfg.cap_per_domain:
            # pre-flight, BEFORE any fetch: the end-of-batch summary tells you
            # about truncation only after the budget is spent
            print(f"# pre-flight: {n} URLs on {d} > cap-per-domain "
                  f"{cfg.cap_per_domain} — cached pages are free, but new "
                  f"fetches will truncate; pass --cap-per-domain {n} to fetch "
                  f"them all", file=sys.stderr)
    for u in args.urls:
        page = fetcher.fetch(u, force_refresh=args.refresh,
                             force_playwright=args.playwright)
        print(f"[{page.method}{'/cache' if page.from_cache else ''}] "
              f"{page.status} {page.health} {page.url} ({len(page.html)} chars)")
    for line in fetcher.cap_summary():
        print(f"\n!! CAPPED: {line}", file=sys.stderr)


def cmd_sitemap(args):
    cfg, store, meter, fetcher, _ = build(args)
    from collections import Counter
    from websurf import sitemaps
    found, files = sitemaps.discover(fetcher, args.site)
    for sm, h in files:
        print(f"# sitemap [{h}] {sm}", file=sys.stderr)
    if args.filter:
        hits = [u for u in found if args.filter in u]
        for u in hits:
            print(u)
        print(f"# {len(hits)}/{len(found)} URLs match {args.filter!r}", file=sys.stderr)
    else:
        from websurf import urls as urls_mod
        pats = Counter(urls_mod.group_key(u) or u for u in found)
        for p, n in pats.most_common(40):
            print(f"{n}\t{p}")
        print(f"# {len(found)} URLs total; list a pattern's URLs with "
              f"--filter <substring>", file=sys.stderr)


def cmd_text(args):
    cfg, store, meter, fetcher, _ = build(args)
    page = fetcher.fetch(args.url)
    print(proj.main_text(page.html))


def cmd_synthesize(args):
    cfg, store, meter, fetcher, llm = build(args, need_llm=True)
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    samples = []
    sample_html = None
    for u in args.urls[:2]:
        page = fetcher.fetch(u)
        sample_html = sample_html or page.html
        samples.append((page.url, proj.prune_html(page.html, cfg.synth_html_cap // 2)))
    # prune_html strips scripts — embedded JSON travels as its own digest
    islands = proj.data_islands(sample_html)
    spec = llm.ask_json(planner.synth_prompt(args.objective or "extract the fields",
                                             fields, samples, islands=islands),
                        system=planner.SYNTH_SYSTEM, max_tokens=2000,
                        purpose="synthesize")
    ex.validate_spec(spec, sample_html)
    print(json.dumps(spec, indent=2))
    print(f"# valid against {args.urls[0]}; tokens: {meter.tokens}", file=sys.stderr)


def cmd_extract(args):
    cfg, store, meter, fetcher, _ = build(args)
    spec = json.loads(Path(args.spec).read_text())
    validated = False
    for i, u in enumerate(args.urls, 1):
        t0 = time.monotonic()
        # allow_render=False: extract consumes the un-rendered HTML itself
        # (island and meta specs READ the shell) — a spec that needs rendered
        # DOM gets it via an explicit `fetch --playwright` first.
        page = fetcher.fetch(u, allow_render=False)
        if not validated and page.html:
            # Once, against the first fetchable page: a bad spec dies
            # immediately; one odd page mid-batch doesn't kill the run.
            ex.validate_spec(spec, page.html)
            validated = True
        recs = ex.apply_spec(spec, page.html, page.final_url or page.url) \
            if page.html else []
        for rec in recs:
            rec["_source"] = page.url
            print(json.dumps(rec, default=str), flush=True)
        print(f"# [{i}/{len(args.urls)}] {page.health}"
              f"{'/cache' if page.from_cache else ''} {len(recs)} record(s) "
              f"{time.monotonic() - t0:.1f}s {page.url}", file=sys.stderr)
    for line in fetcher.cap_summary():
        print(f"\n!! CAPPED: {line}", file=sys.stderr)


def cmd_export(args):
    _, store, *_ = build(args, readonly=True)
    rows = store.records(args.run)
    out = open(args.out, "w") if args.out else sys.stdout
    for r in rows:
        rec = json.loads(r["data"])
        rec["_source"] = r["source_url"]
        rec["_extractor"] = r["extractor"]
        rec["_run"] = r["run_id"]
        print(json.dumps(rec, default=str), file=out)
    if args.out:
        out.close()
        print(f"wrote {len(rows)} records to {args.out}")


def cmd_runs(args):
    _, store, *_ = build(args, readonly=True)
    for r in store.runs_list():
        print(f"{r['id']}\t{r['state']}\t{r['started_at']}\t{r['objective'][:80]}")


def cmd_report(args):
    _, store, *_ = build(args, readonly=True)
    row = store.run_row(args.run)
    if not row:
        raise SystemExit(f"no run {args.run}")
    if row["report"]:
        _print_report(json.loads(row["report"]))
    else:
        print(f"run {args.run} is {row['state']} (no report yet)")


def cmd_skill(args):
    from websurf import skill
    if args.install is None:
        print(skill.render(), end="")  # template is newline-terminated
    else:
        path = skill.install(args.install or None)
        print(f"wrote {path}")


def cmd_status(args):
    cfg, store, *_ = build(args, readonly=True)
    kind = "cache — stale pages auto-pruned, safe to delete" \
        if cfg.db_path == default_db_path() else "project store"
    size = Path(cfg.db_path).stat().st_size / 1e6
    print(f"store: {cfg.db_path} ({kind}, {size:.1f} MB)")
    conn = store.conn
    for tbl in ("pages", "runs", "records", "extractors"):
        n = conn.execute(f"SELECT count(*) c FROM {tbl}").fetchone()["c"]
        print(f"{tbl}: {n}")
    for r in conn.execute("SELECT health, count(*) c FROM pages GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  pages/{r['health']}: {r['c']}")


def cmd_prune(args):
    cfg, store, *_ = build(args, readonly=True)  # readonly: don't create if absent
    before = Path(cfg.db_path).stat().st_size
    if args.all:
        counts = store.wipe()
        print("wiped: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    else:
        age = args.max_age if args.max_age is not None else cfg.max_age_days
        n = store.prune_pages(age)
        print(f"pruned {n} pages older than {age:g}d "
              f"(runs/records/extractors untouched; use --all to wipe)")
    store.vacuum()
    after = Path(cfg.db_path).stat().st_size
    print(f"{cfg.db_path}: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")


def main():
    ap = argparse.ArgumentParser(
        prog="websurf", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="LLM agents: run `skill` for the full driving guide "
               "(Claude Code skill format, paths resolved for this install); "
               "`skill --install` writes it to ~/.claude/skills/websurf/.")
    ap.add_argument("--db", default=default_db_path(),
                    help="SQLite store (page cache, runs, records, extractors). "
                         "The default (%(default)s) is a per-user CACHE: stale "
                         "pages are pruned automatically and deleting it is always "
                         "safe. For a project dataset pass a per-project path "
                         "(e.g. --db ./acme.db) — explicit stores are durable and "
                         "never auto-pruned. Global flag — put it before the "
                         "subcommand, and use the SAME path for every step of a job.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="autonomous run: probe -> plan -> execute")
    p.add_argument("objective")
    p.add_argument("urls", nargs="+")
    p.add_argument("--fields", help="comma-separated target fields, e.g. name,description,url")
    p.add_argument("--mode", choices=["auto", "manual"])
    p.add_argument("--model")
    p.add_argument("--backend", choices=["auto", "api", "cli"])
    p.add_argument("--cap-per-group", type=int, dest="cap_per_group")
    p.add_argument("--cap-pages", type=int, dest="cap_pages")
    p.add_argument("--cap-per-domain", type=int, dest="cap_per_domain",
                   help="new fetches per domain this run (default 80)")
    p.add_argument("--budget-tokens", type=int, dest="budget_tokens")
    p.add_argument("--budget-renders", type=int, dest="budget_renders",
                   help="browser renders per run (default 400 — a circuit breaker; "
                        "skipped shells are reported loudly, never served as data)")
    p.add_argument("--no-render-breaker", action="store_true", dest="no_render_breaker",
                   help="keep auto-rendering a domain even after most of its renders "
                        "classify soft_404 (SPA serving not-found shells for deleted "
                        "pages); default trips at >=70%% of >=10 renders")
    p.add_argument("--allow-external", action="store_true",
                   help="permit cross-domain fan-out (enrichment) — off by default")
    p.add_argument("--max-age", type=float, help="page cache freshness in days")
    p.add_argument("--min-delay", type=float,
                   help="seconds between fetches to one domain (default 2.0; "
                        "robots crawl-delay still wins if larger)")
    p.add_argument("--json", action="store_true", help="also dump the full report JSON")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("probe", help="fetch one page, print its outline JSON")
    p.add_argument("url")
    p.add_argument("--playwright", action="store_true")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("expand", help="list all links in an outline group ('*' = every link)")
    p.add_argument("url")
    p.add_argument("group", help="group id from probe, e.g. 'host/portfolio/*', or '*' for every link")
    p.set_defaults(fn=cmd_expand)

    p = sub.add_parser("fetch", help="fetch pages into the cache, print health")
    p.add_argument("urls", nargs="+")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--playwright", action="store_true")
    p.add_argument("--cap-per-domain", type=int, dest="cap_per_domain",
                   help="new fetches per domain this invocation (default 80); "
                        "raise deliberately for big single-domain batches")
    p.add_argument("--budget-renders", type=int, dest="budget_renders",
                   help="browser renders this invocation (default 400); skipped "
                        "shells are reported loudly and retried next invocation")
    p.add_argument("--no-render-breaker", action="store_true", dest="no_render_breaker",
                   help="keep auto-rendering a domain even after most of its renders "
                        "classify soft_404 (SPA serving not-found shells for deleted "
                        "pages); default trips at >=70%% of >=10 renders")
    p.add_argument("--min-delay", type=float,
                   help="seconds between fetches to one domain (default 2.0; "
                        "robots crawl-delay still wins if larger)")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("sitemap", help="list a site's sitemap URLs (pattern summary; "
                                       "the URL source for JS-shell listings)")
    p.add_argument("site", help="domain or URL")
    p.add_argument("--filter", help="print URLs containing this substring "
                                    "(default: pattern summary with counts)")
    p.set_defaults(fn=cmd_sitemap)

    p = sub.add_parser("text", help="print the main-text projection of a page")
    p.add_argument("url")
    p.set_defaults(fn=cmd_text)

    p = sub.add_parser("synthesize", help="LLM-write an extractor spec from sample pages")
    p.add_argument("urls", nargs="+")
    p.add_argument("--fields", required=True)
    p.add_argument("--objective")
    p.add_argument("--model")
    p.add_argument("--backend", choices=["auto", "api", "cli"])
    p.set_defaults(fn=cmd_synthesize)

    p = sub.add_parser("extract", help="apply a spec JSON file to pages, print records "
                                       "(never renders — island/meta specs read the "
                                       "un-rendered HTML; `fetch --playwright` first "
                                       "if the spec needs rendered DOM)")
    p.add_argument("urls", nargs="+")
    p.add_argument("--spec", required=True)
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("export", help="dump a run's records as JSONL")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("runs", help="list runs")
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("report", help="print a finished run's report")
    p.add_argument("--run", type=int, required=True)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("skill", help="print the LLM driving guide; --install writes "
                                     "it where Claude Code discovers skills")
    p.add_argument("--install", nargs="?", const="", default=None, metavar="DIR",
                   help="write SKILL.md into DIR (default ~/.claude/skills/websurf/)")
    p.set_defaults(fn=cmd_skill)

    p = sub.add_parser("status", help="store location, cache and run counts")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("prune", help="delete stale cached pages and compact the store")
    p.add_argument("--max-age", type=float, dest="max_age",
                   help="prune pages older than this many days (default 7)")
    p.add_argument("--all", action="store_true",
                   help="wipe everything: pages, runs, records, extractors")
    p.set_defaults(fn=cmd_prune)

    args = ap.parse_args()
    if args.cmd != "skill" and not (
            Path.home() / ".claude" / "skills" / "websurf" / "SKILL.md").exists():
        # one stderr line for agents meeting the tool cold via bash; goes
        # quiet once the skill is installed. Never touches ~/.claude itself.
        print("# agents: `websurf skill` prints the full driving guide; "
              "`websurf skill --install` persists it for future sessions",
              file=sys.stderr)
    args.fn(args)


if __name__ == "__main__":
    main()
