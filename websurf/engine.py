"""The run loop: probe -> plan -> execute -> re-plan on deterministic triggers.

Cost model in one line: LLM cost is O(templates + plan cycles), not O(pages) —
synthesis amortizes extraction, structured data is free, per-page LLM reading
is the metered fallback, and the meter hard-stops everything.

Progress is judged deterministically (new records per cycle); the LLM never
gets to decide whether it is stuck.
"""
import json
import sys
from collections import Counter

from . import extractors as ex
from . import planner
from . import projections as proj
from . import urls as urls_mod
from .llm import BudgetExceeded

# Healths whose pages carry extractable data. data_shell = JS shell with an
# embedded JSON island: extracted via island specs, never rendered.
EXTRACTABLE = ("ok", "data_shell")


class Engine:
    def __init__(self, config, store, fetcher, llm, log=None):
        self.config = config
        self.store = store
        self.fetcher = fetcher
        self.llm = llm
        self.log = log or (lambda msg: print(msg, flush=True))

        self.run_id = None
        self.fields = []
        self.objective = ""
        self.batch_seq = 0
        self.batches = {}          # batch id -> [canonical urls]
        self.outlines = {}         # url -> outline dict
        self.page_health = {}      # url -> health
        self.offered_urls = set()  # anti-hallucination set for plan validation
        self.pending_outlines = [] # outlined-but-not-yet-shown-to-planner urls
        self.clamps = []
        self.notes = []
        self.extractors_used = set()

    # -- top level -------------------------------------------------------------

    def run(self, objective, start_urls, fields):
        self.objective = objective
        self.fields = fields or []
        starts = []
        for u in start_urls:
            c = urls_mod.canonicalize(u if "://" in u else "https://" + u)
            if c and c not in starts:
                starts.append(c)
        self.run_id = self.store.create_run(objective, starts, self.fields,
                                            vars(self.config))
        state, cycles = "done", 0
        try:
            self._probe(starts)
            if self._fast_path_done(starts):
                self.notes.append("fast path: answered directly from start pages")
            else:
                state, cycles = self._cycle_loop()
        except BudgetExceeded as e:
            state = "budget_exceeded"
            self.notes.append(str(e))
            self.log(f"!! {e}")
        report = self._report(state, cycles)
        self.store.finish_run(self.run_id, state, report)
        return report

    def _probe(self, starts):
        pages = []
        for u in starts:
            page = self.fetcher.fetch(u)
            self._register_page(page, outline_it=True)
            pages.append(page)
            self.log(f"probe [{page.method}{'/cache' if page.from_cache else ''}] "
                     f"{page.health} {page.url}")
        self.batches["start"] = [p.url for p in pages]
        self._event("probe", {"urls": starts,
                              "healths": [p.health for p in pages]})

    # -- fast path ---------------------------------------------------------------

    def _fast_path_done(self, starts):
        """<= N start pages + fields requested: read them directly (structured
        data first, LLM second). Falls through to planning if any page turns
        out to be a hub rather than the answer."""
        if not self.fields or len(starts) > self.config.fast_path_pages:
            return False
        needs_nav, extracted = False, 0
        for url in starts:
            if self.page_health.get(url) not in EXTRACTABLE:
                continue
            html = self.store.page_html(url) or ""
            blocks, _ = proj.structured_data(html)
            rec = ex.sd_extract(blocks, self.fields)
            if rec:
                extracted += self._save_records(url, "structured-data", [rec])
                continue
            recs, nav = self._llm_extract(url, html, self.fields)
            if nav:
                needs_nav = True
            extracted += self._save_records(url, "llm", recs)
        if needs_nav or extracted == 0:
            self.notes.append("fast path fell through to planning "
                              f"(needs_navigation={needs_nav}, records={extracted})")
            return False
        return True

    # -- plan/execute cycles -------------------------------------------------------

    def _cycle_loop(self):
        dry = 0
        for cycle in range(1, self.config.max_cycles + 1):
            plan = self.llm.ask_json(
                planner.plan_prompt(self.objective, self.fields,
                                    self._state(cycle), self._outline_payload(),
                                    self.config),
                system=planner.PLAN_SYSTEM.replace(
                    "{max_steps}", str(self.config.max_steps_per_plan)),
                max_tokens=3000, purpose="plan")
            steps, clamps = planner.validate_plan(
                plan, self.offered_urls, set(self.batches), self.config, self.fields)
            self.clamps.extend(clamps)
            for c in clamps:
                self.log(f"clamp: {c}")
            analysis = (plan.get("analysis") or "")[:200] if isinstance(plan, dict) else ""
            self.log(f"cycle {cycle}: {analysis or '(no analysis)'} "
                     f"-> {[s['step'] for s in steps]}")
            self._event("plan", {"cycle": cycle, "steps": steps, "clamps": clamps})

            if not steps:
                dry += 1
                if dry >= 2:
                    return "stuck", cycle
                continue
            new_records, done = self._execute(steps, cycle)
            if done:
                return "done", cycle
            dry = 0 if new_records else dry + 1
            if dry >= 2:
                self.notes.append("two consecutive cycles yielded no new records")
                return "stuck", cycle
        return "max_cycles", self.config.max_cycles

    def _execute(self, steps, cycle):
        new_records, done = 0, False
        last_bid = None

        def resolve(src):
            return last_bid if src == "prev" else src

        for step in steps:
            kind = step["step"]
            if kind == "fetch":
                self.batch_seq += 1
                last_bid = f"b{self.batch_seq}"
                self._do_fetch(step["urls"], last_bid, outline_all=True)
            elif kind == "fetch_group":
                src = resolve(step["from"])
                if src not in self.batches:
                    self.notes.append(f"fetch_group: unknown batch {src!r}")
                    continue
                self.batch_seq += 1
                last_bid = f"b{self.batch_seq}"
                self._do_fetch_group(step, src, last_bid)
            elif kind == "extract":
                src = resolve(step["from"])
                if src not in self.batches:
                    self.notes.append(f"extract: unknown batch {src!r}")
                    continue
                new_records += self._do_extract(step, src)
            elif kind == "enrich":
                self._do_enrich(step)
            elif kind == "done":
                self.notes.append(f"done: {step['reason']}")
                done = True
                break
        return new_records, done

    # -- step handlers ---------------------------------------------------------------

    def _do_fetch(self, urls, bid, outline_all=False):
        pages = []
        for u in urls:
            page = self.fetcher.fetch(u)
            self._register_page(page, outline_it=outline_all)
            pages.append(page)
            self.log(f"fetch [{page.method}{'/cache' if page.from_cache else ''}] "
                     f"{page.health} {page.url}")
        self.batches[bid] = [p.url for p in pages]
        return pages

    def _do_fetch_group(self, step, src, bid):
        src_urls = list(self.batches[src])
        group, cap = step["group"], step["cap"]
        members, seen = [], set()

        def collect(url):
            html = self.store.page_html(url) or ""
            row = self.store.get_page(url)
            base = (row["final_url"] if row else None) or url
            for link in proj.expand_group(html, base, group):
                u = link["url"]
                if u in seen or u in self.page_health:
                    continue
                seen.add(u)
                if urls_mod.is_denied(u):
                    self.clamps.append(f"skipped denylisted link {u}")
                    continue
                if (not self.config.allow_external and
                        urls_mod.canonical_domain(u) != urls_mod.canonical_domain(url)):
                    self.clamps.append(f"skipped cross-domain link {u}")
                    continue
                members.append(u)
            return proj.next_page(html, base)

        for src_url in src_urls:
            nxt = collect(src_url)
            if step.get("paginate"):
                hops = 0
                while nxt and hops < self.config.cap_paginate:
                    if nxt in self.page_health:
                        break
                    page = self.fetcher.fetch(nxt)
                    self._register_page(page, outline_it=False)
                    self.batches[src].append(page.url)
                    self.log(f"paginate [{page.method}] {page.health} {page.url}")
                    hops += 1
                    nxt = collect(page.url) if page.health == "ok" else None

        self.offered_urls.update(members)
        if len(members) > cap:
            self.clamps.append(
                f"group {group!r} had {len(members)} links; fetching first {cap} "
                f"({len(members) - cap} dropped — raise --cap-per-group to widen)")
        take = self._confirm_fanout(group, members, cap)
        pages = self._do_fetch(members[:take], bid, outline_all=False)
        # Outline one representative per template so the planner can see what
        # these pages look like without paying for 30 outlines.
        seen_templates = set()
        for p in pages:
            if p.health not in EXTRACTABLE:
                continue
            t = urls_mod.template_key(p.url)
            if t not in seen_templates:
                seen_templates.add(t)
                self._outline_page(p)

    def _do_extract(self, step, src):
        fields, mode = step["fields"], step["mode"]
        pages = [u for u in self.batches[src] if self.page_health.get(u) in EXTRACTABLE]
        if not pages:
            self.notes.append(f"extract from {src}: no healthy pages")
            return 0
        fields_key = ",".join(sorted(f.lower() for f in fields))
        new = 0

        remaining = []
        for url in pages:
            html = self.store.page_html(url) or ""
            blocks, _ = proj.structured_data(html)
            rec = ex.sd_extract(blocks, fields)
            if rec:
                new += self._save_records(url, "structured-data", [rec])
            else:
                remaining.append(url)
        if not remaining:
            return new

        if mode == "llm":
            for url in remaining:
                recs, _ = self._llm_extract(url, self.store.page_html(url) or "", fields)
                new += self._save_records(url, "llm", recs)
            return new

        clusters = {}
        for url in remaining:
            key = (urls_mod.template_key(url), proj.dom_shape(self.store.page_html(url) or ""))
            clusters.setdefault(key, []).append(url)

        for (template, _shape), cluster in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
            if len(cluster) == 1:  # no amortization to be had — read it directly
                url = cluster[0]
                recs, _ = self._llm_extract(url, self.store.page_html(url) or "", fields)
                new += self._save_records(url, "llm", recs)
                continue
            new += self._extract_cluster(template, cluster, fields, fields_key)
        return new

    def _extract_cluster(self, template, cluster, fields, fields_key):
        domain = urls_mod.canonical_domain(cluster[0])
        row = self.store.get_extractor(domain, template, fields_key)
        spec = json.loads(row["spec"]) if row else None
        sample_html = self.store.page_html(cluster[0]) or ""

        if spec is None:
            spec = self._synthesize(cluster, fields, sample_html)
        applied = self._apply_all(spec, cluster) if spec else None
        if applied is not None:
            flat = [r for recs in applied.values() for r in recs]
            expected = len(cluster) if spec.get("kind") == "detail" else None
            ok, reasons = ex.check_health(flat, fields, expected)
            if not ok:  # repair: one re-synthesis with the failure as feedback
                self.log(f"extractor unhealthy for {template}: {'; '.join(reasons)} — re-synthesizing")
                spec = self._synthesize(cluster, fields, sample_html,
                                        feedback="; ".join(reasons))
                applied = self._apply_all(spec, cluster) if spec else None
                if applied is not None:
                    flat = [r for recs in applied.values() for r in recs]
                    ok, reasons = ex.check_health(flat, fields, expected)

        if applied is None or not ok:
            self.notes.append(f"template {template}: extractor failed "
                              f"({'; '.join(reasons) if applied else 'no valid spec'}) "
                              "— falling back to per-page LLM extraction")
            new = 0
            for url in cluster:
                recs, _ = self._llm_extract(url, self.store.page_html(url) or "", fields)
                new += self._save_records(url, "llm", recs)
            return new

        saved = self.store.save_extractor(domain, template, fields_key, spec, healthy=True)
        label = f"spec:{domain}:{template}#v{saved['version']}"
        self.extractors_used.add(label)
        new = 0
        for url, recs in applied.items():
            new += self._save_records(url, label, recs)
        self.log(f"extracted {sum(len(r) for r in applied.values())} records from "
                 f"{len(cluster)} pages via {label}")
        return new

    def _apply_all(self, spec, cluster):
        try:
            out = {}
            for url in cluster:
                row = self.store.get_page(url)
                base = (row["final_url"] if row else None) or url
                out[url] = ex.apply_spec(spec, self.store.page_html(url) or "", base)
            return out
        except Exception as e:
            self.notes.append(f"spec application error: {e}")
            return None

    def _synthesize(self, cluster, fields, sample_html, feedback=None):
        samples = [(u, proj.prune_html(self.store.page_html(u) or "",
                                       self.config.synth_html_cap // 2))
                   for u in cluster[:2]]
        # prune_html strips scripts, so embedded JSON never reaches the samples —
        # islands travel as their own digest (this is how JS shells synthesize).
        islands = proj.data_islands(sample_html)
        try:
            spec = self.llm.ask_json(
                planner.synth_prompt(self.objective, fields, samples, feedback,
                                     islands=islands),
                system=planner.SYNTH_SYSTEM, max_tokens=2000, purpose="synthesize")
            ex.validate_spec(spec, sample_html)
            return spec
        except ex.SpecError as e:
            self.notes.append(f"synthesized spec invalid: {e}")
            return None
        except BudgetExceeded:
            raise
        except Exception as e:
            self.notes.append(f"synthesis failed: {e}")
            return None

    def _llm_extract(self, url, html, fields):
        text = proj.main_text(html, self.config.extract_text_cap)
        blocks, og = proj.structured_data(html)
        sd_json = json.dumps(blocks + ([og] if og else []), default=str)[:2000] \
            if (blocks or og) else None
        links = None
        if any("url" in f.lower() or "link" in f.lower() for f in fields):
            links = [l for l in proj.page_links(html, url)
                     if l["region"] not in ("nav", "footer")][:120]
        try:
            obj = self.llm.ask_json(
                planner.extract_prompt(fields, url, text, sd_json, links),
                system=planner.EXTRACT_SYSTEM, max_tokens=3000, purpose="extract")
        except BudgetExceeded:
            raise
        except Exception as e:
            self.notes.append(f"llm extraction failed for {url}: {e}")
            return [], False
        recs = [r for r in (obj.get("records") or []) if isinstance(r, dict)]
        return recs, bool(obj.get("needs_navigation"))

    def _do_enrich(self, step):
        url_field, n = step["url_field"], 0
        rows = self.store.records(self.run_id)
        if not self._confirm(f"enrich {len(rows)} records by fetching each "
                             f"record's {url_field!r} domain homepage?"):
            self.clamps.append("enrich skipped by user")
            return
        for row in rows:
            data = json.loads(row["data"])
            target = data.get(url_field)
            if not target or not str(target).startswith("http") or "_enrichment" in data:
                continue
            if urls_mod.is_denied(target):
                continue
            page = self.fetcher.fetch(target)
            if page.health != "ok":
                continue
            o = proj.outline(page, self.config)
            data["_enrichment"] = {
                "title": o["title"], "description": o["meta_description"] or
                (o["og"] or {}).get("og:description"),
                "text_preview": o["text_preview"], "url": page.url,
            }
            self.store.update_record(row["id"], data)
            n += 1
            self.log(f"enrich [{page.method}] {page.url}")
        self.notes.append(f"enriched {n} records via {url_field!r}")

    # -- bookkeeping ---------------------------------------------------------------

    def _register_page(self, page, outline_it):
        self.page_health[page.url] = page.health
        self.offered_urls.add(page.url)
        if outline_it and page.html:
            self._outline_page(page)

    def _outline_page(self, page):
        o = proj.outline(page, self.config)
        self.outlines[page.url] = o
        self.pending_outlines.append(page.url)
        for g in o["links"]["groups"]:
            self.offered_urls.update(u for _, u in g["samples"])
        self.offered_urls.update(s["url"] for s in o["links"]["singles"])
        if o["next_page"]:
            self.offered_urls.add(o["next_page"])

    def _outline_payload(self):
        urls = self.pending_outlines[:self.config.outlines_per_plan]
        omitted = len(self.pending_outlines) - len(urls)
        self.pending_outlines = []
        if omitted:
            self.notes.append(f"{omitted} page outlines omitted from plan context")
        return [self.outlines[u] for u in urls]

    def _state(self, cycle):
        recs = self.store.records(self.run_id)
        return {
            "cycle": cycle, "max_cycles": self.config.max_cycles,
            "records_total": len(recs),
            "records_sample": [json.loads(r["data"]) for r in recs[:3]],
            "batches": {bid: {"pages": len(urls),
                              "healths": dict(Counter(self.page_health.get(u, "?")
                                                      for u in urls))}
                        for bid, urls in self.batches.items()},
            "budget": {"tokens_spent": self.llm.meter.tokens,
                       "tokens_budget": self.config.budget_tokens,
                       "fetches": self.llm.meter.fetches},
            "caps": {"cap_per_group": self.config.cap_per_group,
                     "cap_paginate": self.config.cap_paginate,
                     "allow_external": self.config.allow_external},
            "recent_clamps": self.clamps[-5:],
            "recent_notes": self.notes[-5:],
        }

    def _save_records(self, source_url, extractor, recs):
        n = 0
        for rec in recs:
            n += self.store.add_record(self.run_id, source_url, extractor, rec)
        return n

    def _confirm_fanout(self, group, members, cap):
        take = min(len(members), cap)
        if self.config.mode != "manual" or not sys.stdin.isatty():
            return take
        ans = input(f"Group {group!r}: {len(members)} links (cap {cap}). "
                    f"Fetch how many? [{take}] / number / 0 to skip: ").strip()
        if ans.isdigit():
            take = min(int(ans), len(members), self.config.cap_per_group)
        return take

    def _confirm(self, question):
        if self.config.mode != "manual" or not sys.stdin.isatty():
            return True
        return input(f"{question} [Y/n]: ").strip().lower() not in ("n", "no")

    def _event(self, kind, detail):
        self.store.log_event(self.run_id, kind, detail)

    def _report(self, state, cycles):
        recs = self.store.records(self.run_id)
        self.clamps.extend(self.fetcher.cap_summary())
        return {
            "run_id": self.run_id,
            "state": state,
            "objective": self.objective,
            "fields": self.fields,
            "cycles": cycles,
            "records": len(recs),
            "records_by_extractor": dict(Counter(r["extractor"] for r in recs)),
            "pages_by_health": dict(Counter(self.page_health.values())),
            "extractors_used": sorted(self.extractors_used),
            "clamps": self.clamps,
            "notes": self.notes,
            "meter": self.llm.meter.summary(),
        }
