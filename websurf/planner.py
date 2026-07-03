"""Planner: prompt templates + plan validation/clamping.

The plan is a typed, ordered list of steps from a small vocabulary. It is the
human-approval surface in manual mode. The LLM proposes; validate_plan()
decides — every URL must come from the offered set, every cap is clamped,
gated steps are dropped and logged. Guardrails live here and in the engine,
never in the prompt.
"""
import json

STEP_VOCAB = """Each step is one of:
- {"step": "fetch", "urls": ["<url>", ...]}
    Fetch specific pages. URLs MUST be copied exactly from the outlines
    (singles, group samples, next_page) or from previously fetched pages.
- {"step": "fetch_group", "from": "start"|"b<N>"|"prev", "group": "<group id>",
   "cap": <int>, "paginate": true|false}
    Expand a link group seen in the outlines of batch <from> and fetch its
    members (up to cap). paginate=true also follows the source pages'
    pagination and collects group members from those pages too.
- {"step": "extract", "from": "start"|"b<N>"|"prev", "fields": [...], "mode": "auto"|"llm"}
    Extract records from the pages of a batch. mode "auto" (default):
    structured data first, then a synthesized per-template extractor applied
    deterministically, LLM per-page only as fallback. mode "llm" forces
    per-page LLM reading (use only for hostile/irregular pages).
- {"step": "enrich", "url_field": "<field>", "fields": [...]}
    For each record so far, fetch the external URL in <url_field> and attach
    that page's title/description as record._enrichment (deterministic).
    Only available when external crawling is enabled.
- {"step": "done", "reason": "<why the objective is met or cannot be met>"}

Every fetch/fetch_group step produces a new batch (b1, b2, ... — the run
state lists existing ones). Within the SAME plan, use "from": "prev" to
reference the batch created by the most recent fetch/fetch_group step."""

PLAN_SYSTEM = f"""You are the planner of a web crawler. Deterministic code does all
fetching and parsing; you only decide WHAT to fetch and WHEN to extract.
You are given the objective, page outlines (title, headings, structured data,
link groups WITH COUNTS, singles), and the run state.

Reply with ONLY a JSON object: {{"analysis": "<one or two sentences>",
"steps": [ ... ]}}.

{STEP_VOCAB}

Rules:
- Reference only group ids and URLs that appear in the material given to you.
  Never invent URLs.
- Prefer group fetches over long explicit URL lists.
- Be conservative with caps; the harness clamps them anyway.
- Extract as soon as the target pages are in hand — listing pages often
  already carry the fields; detail pages carry richer versions.
- An outline's "repeats" entries are repeating on-page blocks (count, text
  volume, one sample's text). If a repeat already shows the requested fields
  and its contains_group names a link group, the pages IN HAND are the
  complete cheap source — extract from them; do NOT fetch that group.
- Never plan around "denied" link sets (they are never fetched) or groups
  marked "external": true (clamped unless external crawling is enabled).
- "singles_omitted" is an outline display cap, not missing data — extraction
  always reads full page HTML.
- health "data_shell" is NOT unhealthy: the page is a JS shell whose data
  lives in an embedded JSON island (see the outline's data_islands). Extract
  from such pages normally; never route around them.
- If the outlines show the objective is already satisfiable from fetched
  pages, plan extract then done. If a page is unhealthy (blocked, soft_404),
  route around it or declare done with the reason.
- At most {{max_steps}} steps per plan. You will be re-consulted after
  execution with fresh outlines, so plan only the next obvious moves."""

SYNTH_SYSTEM = """You write declarative extractors for a web crawler. Given
sample HTML from pages sharing one template, produce a JSON extractor spec:

{"kind": "list" | "detail",
 "item_selector": "<CSS selector of the repeating item>",   // list only
 "fields": {"<field>": {"sel": "<CSS selector relative to item>",
                        "attr": "text" | "html" | "href" | "src" | "<attr name>"}}}

or, when the page embeds its data as JSON (a DATA ISLANDS section is provided):

{"kind": "island",
 "island_selector": "<CSS selector of the JSON <script> tag>",
 "path": "<dotted path to the entity array inside the JSON>",
 "fields": {"<field>": "<dotted path within one entity object>"}}

Rules:
- "detail" = the page IS one entity (one record per page, selectors relative
  to the whole document). "list" = repeating items on the page.
- If a DATA ISLANDS section shows the requested entities, PREFER the island
  spec: it reads the page's embedded JSON directly and works even when the
  visible HTML is an empty JS shell. Dotted paths may use numeric segments
  for list indices; a non-numeric segment on a list descends into its first
  element.
- Selectors must be plain CSS supported by Python soupsieve (no :contains).
- Use the most stable-looking hooks: semantic tags, ids, meaningful class
  names. Avoid brittle positional selectors when a class exists.
- If the samples carry microdata (itemscope/itemprop attributes), prefer
  [itemprop=...] selectors — they are the most stable hooks available.
- On detail pages whose visible body is thin, og: meta tags are stable and
  server-rendered even on SPAs: {"sel": "meta[property=og:title]",
  "attr": "content"}.
- An empty "sel" ("") means the item element itself.
- Map every requested field you can find; omit fields genuinely absent.
- Reply with ONLY the JSON spec."""

EXTRACT_SYSTEM = """You extract structured records from a web page for a crawler.
Reply with ONLY JSON: {"records": [{<field>: <value>, ...}, ...],
"needs_navigation": false}

- One record per entity on the page (a detail page yields one record; a
  listing yields many).
- Use null for fields not present. Do not fabricate values.
- If a PAGE LINKS section is provided, use it to ground URL-valued fields
  (match entities to their anchor URLs). Only URLs from that section.
- If the page does NOT itself contain the requested data but clearly links
  to pages that would (e.g. it is a hub/menu page), return
  {"records": [], "needs_navigation": true}."""


def plan_prompt(objective, fields, state, outlines, config):
    return (
        f"OBJECTIVE: {objective}\n"
        f"REQUESTED FIELDS: {json.dumps(fields)}\n\n"
        f"RUN STATE:\n{json.dumps(state, indent=1, default=str)}\n\n"
        f"PAGE OUTLINES:\n{json.dumps(outlines, indent=1, default=str)}\n\n"
        "Produce the plan JSON now."
    )


def synth_prompt(objective, fields, samples, feedback=None, islands=None):
    parts = [f"OBJECTIVE: {objective}", f"FIELDS TO EXTRACT: {json.dumps(fields)}"]
    if feedback:
        parts.append(f"A PREVIOUS SPEC FAILED HEALTH CHECKS: {feedback}\n"
                     "Produce a corrected spec with different selectors.")
    if islands:
        parts.append("DATA ISLANDS (embedded JSON found in sample 1 — prefer an "
                     "'island' spec if the requested entities are here):\n"
                     + json.dumps(islands, indent=1, default=str)[:4000])
    for i, (url, html) in enumerate(samples, 1):
        parts.append(f"--- SAMPLE PAGE {i}: {url} ---\n{html}")
    return "\n\n".join(parts)


def extract_prompt(fields, url, text, sd_json=None, links=None):
    parts = [f"FIELDS: {json.dumps(fields)}", f"PAGE URL: {url}"]
    if sd_json:
        parts.append(f"EMBEDDED STRUCTURED DATA:\n{sd_json}")
    if links:
        lines = "\n".join(f"{l['text']} -> {l['url']}" for l in links)
        parts.append(f"PAGE LINKS (anchor text -> URL):\n{lines}")
    parts.append(f"PAGE TEXT:\n{text}")
    return "\n\n".join(parts)


VALID_STEPS = {"fetch", "fetch_group", "extract", "enrich", "done"}


def validate_plan(plan, offered_urls, known_batches, config, run_fields):
    """Clamp and filter the LLM's plan. Returns (steps, clamps) where clamps
    is the human-readable log of everything dropped or reduced."""
    clamps = []
    steps = []
    has_prev = False
    raw = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(raw, list):
        return [], ["plan had no steps list"]

    def src_ok(src):
        return src in known_batches or (src == "prev" and has_prev)

    for s in raw[:config.max_steps_per_plan]:
        if not isinstance(s, dict) or s.get("step") not in VALID_STEPS:
            clamps.append(f"dropped malformed step: {json.dumps(s, default=str)[:120]}")
            continue
        kind = s["step"]
        if kind == "fetch":
            urls = [u for u in (s.get("urls") or []) if isinstance(u, str)]
            valid = [u for u in urls if u in offered_urls]
            for u in set(urls) - set(valid):
                clamps.append(f"dropped un-offered URL: {u}")
            if not valid:
                continue
            steps.append({"step": "fetch", "urls": valid[:config.cap_per_group]})
            has_prev = True
        elif kind == "fetch_group":
            src = s.get("from", "start")
            if not src_ok(src):
                clamps.append(f"dropped fetch_group from unknown batch {src!r}")
                continue
            cap = min(int(s.get("cap") or config.cap_per_group), config.cap_per_group)
            if s.get("cap") and int(s["cap"]) > cap:
                clamps.append(f"clamped group cap {s['cap']} -> {cap}")
            steps.append({"step": "fetch_group", "from": src,
                          "group": s.get("group"), "cap": cap,
                          "paginate": bool(s.get("paginate"))})
            has_prev = True
        elif kind == "extract":
            src = s.get("from", "start")
            if not src_ok(src):
                clamps.append(f"dropped extract from unknown batch {src!r}")
                continue
            fields = s.get("fields") or run_fields
            mode = s.get("mode") if s.get("mode") in ("auto", "llm") else "auto"
            steps.append({"step": "extract", "from": src, "fields": fields, "mode": mode})
        elif kind == "enrich":
            if not config.allow_external:
                clamps.append("dropped enrich step: external crawling disabled "
                              "(--allow-external to enable)")
                continue
            steps.append({"step": "enrich", "url_field": s.get("url_field", "url"),
                          "fields": s.get("fields") or run_fields})
        elif kind == "done":
            steps.append({"step": "done", "reason": str(s.get("reason", ""))[:300]})
            break  # nothing executes after done
    return steps, clamps
