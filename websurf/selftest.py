"""Offline self-test: unit checks + a full engine run against a local fixture
site with a scripted fake LLM. Zero network, zero tokens.

    .venv/bin/python -m websurf.selftest
"""
import http.server
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websurf import extractors as ex
from websurf import health, projections as proj, sitemaps, urls
from websurf.config import Config
from websurf.engine import Engine
from websurf.fetch import Fetcher, Page
from websurf.llm import LLM, Meter, parse_json_loose
from websurf.store import Store

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)


# --- unit checks --------------------------------------------------------------

def unit_tests():
    print("== units ==")
    c = urls.canonicalize
    check("canon strips fragment+tracking",
          c("https://WWW.Foo.com/a/?utm_source=x&b=2&a=1#frag") == "https://www.foo.com/a?a=1&b=2",
          c("https://WWW.Foo.com/a/?utm_source=x&b=2&a=1#frag"))
    check("canon rejects mailto", c("mailto:x@y.com") is None)
    check("canon resolves relative",
          c("../b.html", base="http://h.com/x/y/a.html") == "http://h.com/x/b.html")
    check("group_key depth>=2", urls.group_key("https://f.com/portfolio/airbnb") == "f.com/portfolio/*")
    check("group_key depth1 is None", urls.group_key("https://f.com/about") is None)
    check("group_key digit norm",
          urls.group_key("https://f.com/companies/12/x") == "f.com/companies/{n}/*")
    check("denylist", urls.is_denied("https://www.linkedin.com/in/x"))

    check("health ok", health.classify(200, "<html><title>Hi</title>x" * 300, "word " * 300) == "ok")
    check("health blocked", health.classify(200, "<html>Just a moment...</html>", "Just a moment") == "blocked")
    check("health 403", health.classify(403, "<html>x</html>", "x") == "blocked")
    check("health soft404",
          health.classify(200, "<title>Page not found</title>", "The page you requested") == "soft_404")
    check("health js_shell",
          health.classify(200, '<div id="root"></div>' + "<script>x</script>" * 500, "") == "js_shell")
    island_blob = json.dumps({"props": {"pageProps": {"companies": [
        {"title": f"Co{i}", "blurb": "x" * 300} for i in range(8)]}}})
    island_html = ('<div id="__next"></div>'
                   f'<script id="__NEXT_DATA__" type="application/json">{island_blob}</script>')
    check("health data_shell: shell with parseable JSON island is data, not dead",
          health.classify(200, island_html, "") == "data_shell")
    check("health js_shell: non-parseable state blob does NOT count as data",
          health.classify(200, '<div id="root"></div><script type="application/json">'
                          + "window.__NUXT__=(function(){" + "x" * 3000 + "})",
                          "") == "js_shell")

    isl = proj.data_islands(island_html)
    check("data_islands: array path, count and keys surfaced",
          isl and isl[0]["selector"] == "script#__NEXT_DATA__"
          and isl[0]["arrays"][0]["path"] == "props.pageProps.companies"
          and isl[0]["arrays"][0]["count"] == 8
          and "title" in isl[0]["arrays"][0]["item_keys"], isl)
    check("data_islands: absent -> None", proj.data_islands("<p>hi</p>") is None)

    ispec = {"kind": "island", "island_selector": "script#__NEXT_DATA__",
             "path": "props.pageProps.companies",
             "fields": {"name": "title", "description": "blurb"}}
    ex.validate_spec(ispec, island_html)
    irecs = ex.apply_spec(ispec, island_html, "https://h.com/portfolio")
    check("island spec applies deterministically",
          len(irecs) == 8 and irecs[0]["name"] == "Co0", irecs[:2])
    deep_html = ('<script type="application/json">' + json.dumps(
        {"items": [{"n": "A", "site": {"url": "/companies/a"}, "tags": ["ai", "b2b"]}] * 4
    }) + "</script>" + '<script id="pad" type="text/template">' + "x" * 2000 + "</script>")
    dspec = {"kind": "island", "island_selector": 'script[type="application/json"]',
             "path": "items",
             "fields": {"name": "n", "url": "site.url", "tag": "tags.0"}}
    drecs = ex.apply_spec(dspec, deep_html, "https://h.com/x")
    check("island spec: nested paths, numeric index, root-relative URL absolutized",
          drecs and drecs[0]["url"] == "https://h.com/companies/a"
          and drecs[0]["tag"] == "ai", drecs[:1])
    try:
        ex.validate_spec({"kind": "island", "island_selector": "script#nope",
                          "path": "a.b", "fields": {"x": "y"}}, island_html)
        check("island spec: dead path rejected", False)
    except ex.SpecError:
        check("island spec: dead path rejected", True)

    check("parse_json_loose fences",
          parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1})
    check("parse_json_loose prose",
          parse_json_loose('Here is the plan:\n{"a": [1,2]} hope that helps') == {"a": [1, 2]})

    html = """<div><div class="card"><h3>Foo</h3><p class="d">does x</p>
              <a href="/companies/foo">more</a></div>
              <div class="card"><h3>Bar</h3><p class="d">does y</p>
              <a href="/companies/bar">more</a></div></div>"""
    spec = {"kind": "list", "item_selector": "div.card",
            "fields": {"name": {"sel": "h3", "attr": "text"},
                       "description": {"sel": "p.d", "attr": "text"},
                       "url": {"sel": "a", "attr": "href"}}}
    ex.validate_spec(spec, html)
    recs = ex.apply_spec(spec, html, "https://h.com/portfolio")
    check("apply_spec list", len(recs) == 2 and recs[0]["name"] == "Foo"
          and recs[1]["url"] == "https://h.com/companies/bar", recs)
    ok, _ = ex.check_health(recs, ["name", "description", "url"])
    check("check_health ok", ok)
    ok, reasons = ex.check_health([{"name": None}] * 5, ["name"])
    check("check_health nulls fail", not ok, reasons)

    sd = [{"@type": "Product", "name": "Widget", "description": "A widget.",
           "offers": {"@type": "Offer", "price": "9.99", "priceCurrency": "USD",
                      "availability": "https://schema.org/InStock"}}]
    rec = ex.sd_extract(sd, ["name", "price", "availability", "description"])
    check("sd_extract product", rec and rec["price"] == "9.99"
          and "InStock" in rec["availability"], rec)
    check("sd_extract miss", ex.sd_extract(sd, ["ceo", "founded"]) is None)
    org = [{"@type": "Organization", "name": "Fixture Capital",
            "description": "A VC firm.", "url": "https://fixture.vc"}]
    check("sd_extract skips site-self-description nodes",
          ex.sd_extract(org, ["name", "description", "url"]) is None)

    # planner-facing outline signals: denied/external/omitted/repeats/microdata
    cfg = Config()
    lk_base = "https://h.com/portfolio"
    lk_html = ("<main>" + "".join(
        f'<div class="card"><h3><a href="/companies/{i}">C{i}</a></h3>'
        f'<p class="d">C{i} builds thing {i} for sector-{i} teams.</p></div>'
        for i in range(1, 13))
        + "".join(f'<a href="https://www.linkedin.com/company/c{i}">Li{i}</a>'
                  for i in range(5))
        + "".join(f'<a href="https://other.com/team/{i}">T{i}</a>' for i in range(1, 6))
        + "</main><footer>"
        + "".join(f'<a href="/f{i}">F{i}</a>' for i in range(60)) + "</footer>")
    links = proj.page_links(lk_html, lk_base)
    groups, singles, omitted, denied = proj.group_links(links, cfg, "h.com")
    check("denied links summarized, never offered",
          denied == {"linkedin.com/company/*": 5}
          and not any("linkedin" in g["id"] for g in groups), denied)
    g_int = next(g for g in groups if g["id"] == "h.com/companies/*")
    g_ext = next(g for g in groups if g["id"] == "other.com/team/*")
    check("external flagged and sorted last",
          g_ext["external"] and not g_int["external"] and groups[-1] is g_ext)
    check("singles omitted = display-cap summary, not loss",
          omitted["count"] == 20 and omitted["by_region"] == {"footer": 20}
          and "expand" in omitted["note"], omitted)
    check("expand '*' lists every link",
          len(proj.expand_group(lk_html, lk_base, "*")) == len(links))

    reps = proj.repeats(lk_html, lk_base, [g["id"] for g in groups])
    check("repeats: content block found, tied to its group",
          reps and reps[0]["selector"] == "div.card" and reps[0]["count"] == 12
          and reps[0]["contains_group"] == "h.com/companies/*"
          and reps[0]["avg_text_chars"] > 20
          and "builds thing" in reps[0]["sample_text"], reps)

    md_html = ('<div itemscope itemtype="https://schema.org/CreativeWork">'
               '<span itemprop="text">Hi</span><small itemprop="author">X</small></div>')
    md = proj.microdata_summary(proj._soup(md_html))
    check("microdata detected (not parsed)",
          md == {"scopes": 1, "types": ["schema.org/CreativeWork"],
                 "props": ["author", "text"]}, md)
    check("microdata absent -> None", proj.microdata_summary(proj._soup(html)) is None)
    check("prune_html keeps itemprop hooks", "itemprop" in proj.prune_html(md_html))

    # storage: the default store is a per-user cache, prunable and disposable
    from websurf.config import default_db_path
    ddb = Path(default_db_path())
    check("default store: absolute path in a user cache dir, not the package",
          ddb.is_absolute() and ddb.name == "cache.db"
          and "websurf" in str(ddb.parent).lower()
          and not str(ddb).startswith(str(Path(__file__).resolve().parent.parent)),
          ddb)
    st = Store(":memory:")
    st.save_page("https://p.com/old", "https://p.com/old", "p.com", 200,
                 "requests", "ok", "<html>old</html>")
    st.save_page("https://p.com/new", "https://p.com/new", "p.com", 200,
                 "requests", "ok", "<html>new</html>")
    st.conn.execute("UPDATE pages SET fetched_at='2000-01-01 00:00:00' "
                    "WHERE url='https://p.com/old'")
    st.conn.commit()
    check("prune_pages deletes only stale rows",
          st.prune_pages(7.0) == 1
          and st.get_page("https://p.com/new") is not None
          and st.get_page("https://p.com/old") is None)
    st.add_record(1, "https://p.com/new", "t", {"a": 1})
    wiped = st.wipe()
    check("wipe clears every table and reports counts",
          wiped["pages"] == 1 and wiped["records"] == 1
          and st.get_page("https://p.com/new") is None, wiped)
    st.close()

    from websurf import skill as skill_mod
    rendered = skill_mod.render()
    check("skill renders: frontmatter + resolved paths, no sentinels",
          rendered.startswith("---\nname: websurf")
          and sys.executable in rendered and str(skill_mod.PKG) in rendered
          and default_db_path() in rendered
          and "__PY__" not in rendered and "__PKG__" not in rendered
          and "__ROOT__" not in rendered and "__CACHE__" not in rendered)
    sdir = Path(tempfile.mkdtemp(prefix="websurfskill_"))
    try:
        dest = skill_mod.install(sdir)
        check("skill --install writes SKILL.md",
              dest.name == "SKILL.md" and dest.read_text() == rendered, dest)
    finally:
        shutil.rmtree(sdir, ignore_errors=True)


# --- fixture site ---------------------------------------------------------------

CARD = '<div class="company-card"><h3><a href="/companies/{i}.html">{name}</a></h3><p class="blurb">{blurb}</p></div>'
DETAIL = """<html><head><title>{name} — Portfolio</title></head><body>
<h1>{name}</h1>
<p class="desc">{name} builds {what} for {who}. Founded to make {what} effortless,
the team ships weekly and works with design partners across the industry.</p>
<a class="site" href="https://{slug}.example">Visit website</a>
</body></html>"""

COMPANIES = [(i, f"Company{i}", f"agentic tool #{i}", f"sector-{i % 4} teams",
              f"company{i}") for i in range(1, 19)]


def build_fixtures(root):
    (root / "companies").mkdir(parents=True)
    nav = '<nav><a href="/portfolio.html">Portfolio</a> <a href="/about.html">About</a></nav>'
    footer = ('<footer><a href="/privacy.html">Privacy</a>'
              '<a href="https://twitter.com/x">Twitter</a></footer>')
    (root / "index.html").write_text(
        f"<html><head><title>Fixture Capital</title></head><body>{nav}"
        "<main><h1>Fixture Capital</h1><p>" + "We back ambitious AI teams. " * 30
        + f"</p></main>{footer}</body></html>")
    (root / "about.html").write_text(
        f"<html><head><title>About</title></head><body>{nav}<main><h1>About us</h1>"
        "<p>" + "A very serious firm. " * 60 + f"</p></main>{footer}</body></html>")

    cards1 = "".join(CARD.format(i=i, name=n, blurb=f"{n} builds {w}.")
                     for i, n, w, _, _ in COMPANIES[:12])
    cards2 = "".join(CARD.format(i=i, name=n, blurb=f"{n} builds {w}.")
                     for i, n, w, _, _ in COMPANIES[12:])
    (root / "portfolio.html").write_text(
        f"<html><head><title>Portfolio</title></head><body>{nav}<main><h1>Our companies</h1>"
        f"{cards1}<a rel=\"next\" href=\"/portfolio2.html\">Next</a></main>{footer}</body></html>")
    (root / "portfolio2.html").write_text(
        f"<html><head><title>Portfolio p2</title></head><body>{nav}<main><h1>Our companies</h1>"
        f"{cards2}</main>{footer}</body></html>")
    for i, name, what, who, slug in COMPANIES:
        (root / "companies" / f"{i}.html").write_text(
            DETAIL.format(name=name, what=what, who=who, slug=slug))

    # JS-shell pages: app.html ships its data as a Next-style island (data_shell);
    # shell.html is a genuinely empty shell (js_shell — needs a render).
    island = {"props": {"pageProps": {"companies": [
        {"title": n, "summary": f"{n} builds {w} for {who}. " * 6,
         "website": {"url": f"/companies/{i}.html"}}
        for i, n, w, who, _ in COMPANIES[:6]]}}}
    (root / "app.html").write_text(
        '<html><head><title>Portfolio</title></head><body><div id="__next"></div>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(island)}'
        "</script></body></html>")
    (root / "shell.html").write_text(
        '<html><head><title>App</title></head><body><div id="root"></div>'
        + "<script>x</script>" * 500 + "</body></html>")

    sd = {"@context": "https://schema.org", "@type": "Product", "name": "Acme Anvil",
          "description": "A very heavy anvil for very fast coyotes.",
          "offers": {"@type": "Offer", "price": "129.00", "priceCurrency": "USD",
                     "availability": "https://schema.org/InStock"}}
    (root / "product.html").write_text(
        "<html><head><title>Acme Anvil</title>"
        f"<script type=\"application/ld+json\">{json.dumps(sd)}</script></head>"
        "<body><main><h1>Acme Anvil</h1><p>" + "Buy this anvil. " * 60
        + "</p></main></body></html>")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


# --- fake LLM ---------------------------------------------------------------------

class FakeBackend:
    """Scripted planner/synthesizer standing in for the model."""

    def __init__(self, base):
        self.base = base
        self.plan_calls = 0
        self.calls = 0

    def ask(self, prompt, system, max_tokens):
        self.calls += 1
        system = system or ""
        if "planner of a web crawler" in system:
            self.plan_calls += 1
            if self.plan_calls == 1:
                out = {"analysis": "index is a hub; the Portfolio page holds the companies",
                       "steps": [{"step": "fetch", "urls": [f"{self.base}/portfolio.html"]}]}
            else:
                out = {"analysis": "fan out to detail pages, extract, try to enrich",
                       "steps": [
                           {"step": "fetch_group", "from": "b1",
                            "group": "127.0.0.1/companies/*", "cap": 30, "paginate": True},
                           {"step": "extract", "from": "prev",
                            "fields": ["name", "description", "url"]},
                           {"step": "enrich", "url_field": "url", "fields": []},
                           {"step": "fetch", "urls": ["https://invented.example/nope"]},
                           {"step": "done", "reason": "portfolio extracted"}]}
            return json.dumps(out), 500, 100
        if "declarative extractors" in system:
            spec = {"kind": "detail",
                    "fields": {"name": {"sel": "h1", "attr": "text"},
                               "description": {"sel": "p.desc", "attr": "text"},
                               "url": {"sel": "a.site", "attr": "href"}}}
            return json.dumps(spec), 800, 80
        if "extract structured records" in system:
            return json.dumps({"records": [], "needs_navigation": True}), 300, 30
        raise AssertionError(f"unexpected LLM call: {system[:60]}")


# --- integration ---------------------------------------------------------------------

def integration_tests():
    print("== integration ==")
    tmp = Path(tempfile.mkdtemp(prefix="websurftest_"))
    fixtures = tmp / "site"
    fixtures.mkdir()
    build_fixtures(fixtures)

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *a: Quiet(*a, directory=str(fixtures)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    try:
        # -- use case A: hub -> listing -> detail fan-out -> synthesized extractor
        cfg = Config(db_path=str(tmp / "a.db"), min_delay=0.01, mode="auto")
        store = Store(cfg.db_path)
        meter = Meter(cfg)
        fake = FakeBackend(base)
        engine = Engine(cfg, store, Fetcher(cfg, store, meter),
                        LLM(cfg, meter, backend=fake), log=lambda m: None)
        report = engine.run("find the portfolio companies and their descriptions",
                            [f"{base}/"], ["name", "description", "url"])

        check("A: state done", report["state"] == "done", report["state"])
        check("A: 18 records", report["records"] == 18, report["records"])
        recs = [json.loads(r["data"]) for r in store.records(report["run_id"])]
        c7 = next(r for r in recs if r["name"] == "Company7")
        check("A: detail description extracted",
              "agentic tool #7" in c7["description"], c7)
        check("A: url absolutized", c7["url"] == "https://company7.example", c7["url"])
        check("A: extractor synthesized once",
              any(k.startswith("spec:") for k in report["records_by_extractor"])
              and len(report["extractors_used"]) == 1, report["records_by_extractor"])
        check("A: pagination followed",
              any("portfolio2" in u for u in engine.page_health), None)
        check("A: enrich clamped (external off)",
              any("enrich" in c for c in report["clamps"]), report["clamps"])
        check("A: invented URL rejected",
              any("un-offered" in c for c in report["clamps"]), report["clamps"])
        check("A: cross-domain detail links not fetched",
              not any("example" in u for u in engine.page_health))
        check("A: extractor cached",
              store.get_extractor("127.0.0.1", "127.0.0.1/companies/*",
                                  "description,name,url") is not None)
        o = engine.outlines[f"{base}/portfolio.html"]
        check("A: outline repeats tie cards to their detail group",
              o["repeats"] and o["repeats"][0]["selector"] == "div.company-card"
              and o["repeats"][0]["contains_group"] == "127.0.0.1/companies/*",
              o["repeats"])
        # fast path probes the single start page (1 extract call, returns
        # needs_navigation), then 2 plan calls + 1 synthesis. 18 detail pages
        # cost ZERO extract tokens — that's the amortization thesis.
        check("A: llm spend bounded (probe+plan+synth, no per-page reads)",
              fake.plan_calls == 2
              and meter.tokens_by_purpose["extract"] == [300, 30]
              and meter.tokens_by_purpose["synthesize"] == [800, 80],
              meter.summary())

        # -- re-run resumability: all pages served from cache
        meter2 = Meter(cfg)
        fake2 = FakeBackend(base)
        engine2 = Engine(cfg, store, Fetcher(cfg, store, meter2),
                         LLM(cfg, meter2, backend=fake2), log=lambda m: None)
        report2 = engine2.run("find the portfolio companies and their descriptions",
                              [f"{base}/"], ["name", "description", "url"])
        check("A2: re-run hits cache", meter2.fetches == 0 and meter2.cache_hits >= 20,
              meter2.summary())
        check("A2: cached extractor reused (no synth call)",
              meter2.tokens_by_purpose.get("synthesize") is None, meter2.summary())

        # -- use case B: product page, structured data, zero LLM tokens
        cfg_b = Config(db_path=str(tmp / "b.db"), min_delay=0.01)
        store_b = Store(cfg_b.db_path)
        meter_b = Meter(cfg_b)
        engine_b = Engine(cfg_b, store_b, Fetcher(cfg_b, store_b, meter_b),
                          LLM(cfg_b, meter_b, backend=FakeBackend(base)),
                          log=lambda m: None)
        report_b = engine_b.run("get price and availability",
                                [f"{base}/product.html"],
                                ["name", "price", "availability", "description"])
        check("B: fast path done", report_b["state"] == "done", report_b)
        check("B: structured-data record",
              report_b["records_by_extractor"] == {"structured-data": 1}, report_b)
        rec = json.loads(store_b.records(report_b["run_id"])[0]["data"])
        check("B: price value", rec["price"] == "129.00", rec)
        check("B: ZERO llm calls", meter_b.llm_calls == 0, meter_b.summary())

        # -- fast path falls through on a hub page
        cfg_c = Config(db_path=str(tmp / "c.db"), min_delay=0.01, max_cycles=1)
        store_c = Store(cfg_c.db_path)
        meter_c = Meter(cfg_c)
        fake_c = FakeBackend(base)
        engine_c = Engine(cfg_c, store_c, Fetcher(cfg_c, store_c, meter_c),
                          LLM(cfg_c, meter_c, backend=fake_c), log=lambda m: None)
        report_c = engine_c.run("find the portfolio companies",
                                [f"{base}/"], ["name", "description"])
        check("C: hub triggers needs_navigation fallthrough",
              any("fell through" in n for n in report_c["notes"]), report_c["notes"])
        check("C: planning engaged after fallthrough", fake_c.plan_calls >= 1)

        # -- per-domain ceiling: invocation-scoped, overridable, loud
        cfg_d = Config(db_path=str(tmp / "d.db"), min_delay=0.01, cap_per_domain=3)
        store_d = Store(cfg_d.db_path)
        f1 = Fetcher(cfg_d, store_d, Meter(cfg_d))
        urls_d = [f"{base}/companies/{i}.html" for i in range(1, 6)]
        healths = [f1.fetch(u).health for u in urls_d]
        check("D: ceiling clamps within an invocation",
              healths == ["ok"] * 3 + ["domain_capped"] * 2, healths)
        check("D: cap_summary is loud and counts the loss",
              len(f1.cap_summary()) == 1 and "2 URL(s)" in f1.cap_summary()[0]
              and "INCOMPLETE" in f1.cap_summary()[0], f1.cap_summary())
        # fresh invocation, same store: cached pages are free, budget is reset —
        # the 3 cached pages don't eat it (cumulative-forever would brick here)
        f2 = Fetcher(cfg_d, store_d, Meter(cfg_d))
        h2 = [f2.fetch(u).health for u in urls_d]
        check("D: fresh invocation not bricked by cached pages",
              h2 == ["ok"] * 5 and not f2.cap_summary(), h2)
        # explicit raise fetches everything in one go
        cfg_d2 = Config(db_path=str(tmp / "d2.db"), min_delay=0.01, cap_per_domain=5)
        f3 = Fetcher(cfg_d2, Store(cfg_d2.db_path), Meter(cfg_d2))
        h3 = [f3.fetch(u).health for u in urls_d]
        check("D: raised cap fetches the full batch",
              h3 == ["ok"] * 5 and not f3.cap_summary(), h3)

        # -- E: data islands — a JS shell that carries its data is extracted,
        #       never rendered (zero renders, zero tokens)
        cfg_e = Config(db_path=str(tmp / "e.db"), min_delay=0.01)
        store_e = Store(cfg_e.db_path)
        meter_e = Meter(cfg_e)
        f_e = Fetcher(cfg_e, store_e, meter_e)
        p_app = f_e.fetch(f"{base}/app.html")
        check("E: island shell classified data_shell, no render attempted",
              p_app.health == "data_shell" and p_app.method == "requests"
              and meter_e.renders == 0, (p_app.health, meter_e.renders))
        o_app = proj.outline(p_app, cfg_e)
        check("E: outline surfaces the island (path/count/keys)",
              o_app["data_islands"]
              and o_app["data_islands"][0]["arrays"][0]["path"] == "props.pageProps.companies"
              and o_app["data_islands"][0]["arrays"][0]["count"] == 6,
              o_app["data_islands"])
        espec = {"kind": "island", "island_selector": "script#__NEXT_DATA__",
                 "path": "props.pageProps.companies",
                 "fields": {"name": "title", "description": "summary",
                            "url": "website.url"}}
        ex.validate_spec(espec, p_app.html)
        erecs = ex.apply_spec(espec, p_app.html, p_app.url)
        check("E: island extraction — full records incl. absolutized URL",
              len(erecs) == 6 and erecs[0]["name"] == "Company1"
              and erecs[0]["url"] == f"{base}/companies/1.html", erecs[:1])

        # -- F: render budget — skips are loud, cached shells heal, never silent
        cfg_f = Config(db_path=str(tmp / "f.db"), min_delay=0.01, budget_renders=0)
        store_f = Store(cfg_f.db_path)
        f_f = Fetcher(cfg_f, store_f, Meter(cfg_f))
        p_shell = f_f.fetch(f"{base}/shell.html")
        check("F: budget-exhausted shell kept but counted, summary is loud",
              p_shell.health == "js_shell"
              and len(f_f.cap_summary()) == 1
              and "UN-RENDERED" in f_f.cap_summary()[0]
              and "INCOMPLETE" in f_f.cap_summary()[0], f_f.cap_summary())
        # new invocation WITH budget: the cached un-rendered shell is healed
        # (re-fetched with the browser), not served from cache for a week
        cfg_f2 = Config(db_path=str(tmp / "f.db"), min_delay=0.01)
        f_heal = Fetcher(cfg_f2, store_f, Meter(cfg_f2))
        rendered = ("<html><head><title>App</title></head><body><main>"
                    + "rendered content word " * 100 + "</main></body></html>")
        f_heal._fetch_playwright = lambda url: Page(
            url=url, final_url=url, status=200, html=rendered, method="playwright")
        p_healed = f_heal.fetch(f"{base}/shell.html")
        check("F: cache-heal renders the shell instead of serving it",
              not p_healed.from_cache and p_healed.method == "playwright"
              and p_healed.health == "ok", (p_healed.method, p_healed.health))
        p_cached = Fetcher(cfg_f2, store_f, Meter(cfg_f2)).fetch(f"{base}/shell.html")
        check("F: healed page served from cache thereafter",
              p_cached.from_cache and p_cached.health == "ok", p_cached.health)
        # allow_render=False (the extract path): a cached un-rendered shell is
        # served AS-IS — island/meta specs read the shell; healing it into a
        # multi-second render per page would turn spec application into a crawl
        cfg_f3 = Config(db_path=str(tmp / "f3.db"), min_delay=0.01, budget_renders=0)
        store_f3 = Store(cfg_f3.db_path)
        Fetcher(cfg_f3, store_f3, Meter(cfg_f3)).fetch(f"{base}/shell.html")
        cfg_f4 = Config(db_path=str(tmp / "f3.db"), min_delay=0.01)  # budget back
        m_nr = Meter(cfg_f4)
        p_nr = Fetcher(cfg_f4, store_f3, m_nr).fetch(f"{base}/shell.html",
                                                     allow_render=False)
        check("F: allow_render=False serves the cached shell, no cache-heal",
              p_nr.from_cache and p_nr.method == "requests" and m_nr.renders == 0,
              (p_nr.from_cache, p_nr.method, m_nr.renders))
        cfg_f5 = Config(db_path=str(tmp / "f5.db"), min_delay=0.01)
        m_nr2 = Meter(cfg_f5)
        p_nr2 = Fetcher(cfg_f5, Store(cfg_f5.db_path), m_nr2).fetch(
            f"{base}/shell.html", allow_render=False)
        check("F: allow_render=False fresh fetch keeps the requests shell",
              p_nr2.method == "requests" and p_nr2.health == "js_shell"
              and m_nr2.renders == 0, (p_nr2.method, p_nr2.health, m_nr2.renders))

        # -- G: sitemap discovery — robots.txt -> index -> urlset, patterns out
        (fixtures / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap-index.xml\n")
        (fixtures / "sitemap-index.xml").write_text(
            '<?xml version="1.0"?><sitemapindex><sitemap>'
            f"<loc>{base}/sitemap-0.xml</loc></sitemap></sitemapindex>")
        (fixtures / "sitemap-0.xml").write_text(
            '<?xml version="1.0"?><urlset>'
            + "".join(f"<url><loc>{base}/companies/{i}.html</loc></url>"
                      for i in range(1, 19))
            + f"<url><loc>{base}/about.html</loc></url></urlset>")
        cfg_g = Config(db_path=str(tmp / "g.db"), min_delay=0.01)
        f_g = Fetcher(cfg_g, Store(cfg_g.db_path), Meter(cfg_g))
        found, files = sitemaps.discover(f_g, base)
        check("G: sitemap chain followed (robots -> index -> urlset)",
              len(files) == 2 and found and len(found) == 19, (files, len(found)))
        check("G: entity URL pattern visible in the found set",
              sum(1 for u in found
                  if urls.group_key(u) == "127.0.0.1/companies/*") == 18, found[:3])

        # -- H: CLI extract/fetch — records stream, one odd page doesn't abort
        #       the batch, caps are loud (summary AND pre-flight)
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from types import SimpleNamespace
        from websurf.__main__ import cmd_extract, cmd_fetch
        spec_path = tmp / "spec.json"
        spec_path.write_text(json.dumps(
            {"kind": "list", "item_selector": "div.company-card",
             "fields": {"name": {"sel": "h3", "attr": "text"},
                        "url": {"sel": "a", "attr": "href"}}}))
        h_args = SimpleNamespace(
            db=str(tmp / "h.db"), spec=str(spec_path), min_delay=0.01,
            cap_per_domain=3,
            urls=[f"{base}/portfolio.html", f"{base}/about.html",
                  f"{base}/portfolio2.html", f"{base}/shell.html"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cmd_extract(h_args)
        h_recs = [json.loads(line) for line in out.getvalue().splitlines()]
        check("H: extract streams records with provenance",
              len(h_recs) == 18 and h_recs[0]["name"] == "Company1"
              and all(r["_source"] for r in h_recs), len(h_recs))
        check("H: non-matching page mid-batch yields 0 records, batch continues",
              "[2/4]" in err.getvalue() and "0 record(s)" in err.getvalue()
              and "[4/4]" in err.getvalue(), err.getvalue())
        check("H: extract prints the cap summary (capped tail is never silent)",
              "!! CAPPED" in err.getvalue(), err.getvalue())
        f_args = SimpleNamespace(
            db=str(tmp / "h2.db"), refresh=False, playwright=False,
            min_delay=0.01, cap_per_domain=2,
            urls=[f"{base}/companies/{i}.html" for i in range(1, 5)])
        out2, err2 = io.StringIO(), io.StringIO()
        with redirect_stdout(out2), redirect_stderr(err2):
            cmd_fetch(f_args)
        check("H: fetch pre-flight warns before spending the budget",
              "pre-flight" in err2.getvalue()
              and "--cap-per-domain 4" in err2.getvalue(), err2.getvalue())

        # -- I: render breaker — a domain whose renders keep classifying
        #       soft_404 (SPA serving 200 + not-found shells for deleted pages)
        #       stops being auto-rendered; explicit --playwright still forces
        notfound = ("<html><head><title>Page not found</title></head><body>"
                    "<main>" + "Sorry, nothing to see here. " * 30
                    + "</main></body></html>")
        cfg_i = Config(db_path=str(tmp / "i.db"), min_delay=0.01,
                       cap_per_domain=200)
        m_i = Meter(cfg_i)
        f_i = Fetcher(cfg_i, Store(cfg_i.db_path), m_i)
        pw_calls = []
        f_i._fetch_playwright = lambda url: (pw_calls.append(url) or Page(
            url=url, final_url=url, status=200, html=notfound, method="playwright"))
        err_i = io.StringIO()
        with redirect_stderr(err_i):
            healths_i = [f_i.fetch(f"{base}/shell.html?i={i}").health
                         for i in range(14)]
        check("I: breaker trips after min renders at the soft_404 ratio",
              len(pw_calls) == cfg_i.render_breaker_min
              and healths_i[:10] == ["soft_404"] * 10
              and healths_i[10:] == ["js_shell"] * 4, (len(pw_calls), healths_i))
        check("I: skipped shells counted and summary is loud",
              f_i.render_broken == {"127.0.0.1": 4}
              and any("render breaker" in line and "4 further shell(s)" in line
                      for line in f_i.cap_summary()), f_i.cap_summary())
        check("I: trip announced on stderr at trip time",
              "render breaker tripped" in err_i.getvalue(), err_i.getvalue())
        p_force = f_i.fetch(f"{base}/shell.html?i=99", force_playwright=True)
        check("I: explicit --playwright bypasses the breaker",
              len(pw_calls) == cfg_i.render_breaker_min + 1
              and p_force.health == "soft_404", (len(pw_calls), p_force.health))
        cfg_i2 = Config(db_path=str(tmp / "i2.db"), min_delay=0.01,
                        cap_per_domain=200, render_breaker=False)
        f_i2 = Fetcher(cfg_i2, Store(cfg_i2.db_path), Meter(cfg_i2))
        pw2 = []
        f_i2._fetch_playwright = lambda url: (pw2.append(url) or Page(
            url=url, final_url=url, status=200, html=notfound, method="playwright"))
        [f_i2.fetch(f"{base}/shell.html?i={i}") for i in range(12)]
        check("I: --no-render-breaker keeps rendering",
              len(pw2) == 12 and not f_i2.render_broken, len(pw2))
        # live pages interleaved: renders that come back healthy hold the
        # ratio below the trip line — the breaker never fires on a live site
        cfg_i3 = Config(db_path=str(tmp / "i3.db"), min_delay=0.01,
                        cap_per_domain=200)
        f_i3 = Fetcher(cfg_i3, Store(cfg_i3.db_path), Meter(cfg_i3))
        live = ("<html><head><title>Co</title></head><body><main>"
                + "real content word " * 100 + "</main></body></html>")
        pw3 = []
        f_i3._fetch_playwright = lambda url: (pw3.append(url) or Page(
            url=url, final_url=url, status=200, method="playwright",
            html=live if len(pw3) % 2 else notfound))
        [f_i3.fetch(f"{base}/shell.html?i={i}") for i in range(14)]
        check("I: 50% soft_404 stays under the ratio — no trip",
              len(pw3) == 14 and not f_i3.render_broken, (len(pw3), f_i3.render_broken))

        # -- J: playwright teardown — close() is deterministic and idempotent
        #       (skipping it segfaults greenlet during interpreter finalization)
        class FakePW:
            def __init__(self):
                self.n = 0
            def close(self):
                self.n += 1
            def stop(self):
                self.n += 1
        f_j = Fetcher(cfg_i, Store(str(tmp / "j.db")), Meter(cfg_i))
        fb, fp = FakePW(), FakePW()
        f_j._tl.browser, f_j._tl.pw = fb, fp
        f_j.close()
        f_j.close()
        check("J: close() closes browser + stops playwright exactly once",
              fb.n == 1 and fp.n == 1, (fb.n, fp.n))
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unit_tests()
    integration_tests()
    print(f"\nall {PASS} checks passed")
