# websurf

websurf is a command-line web browsing and extraction instrument built to be driven by AI agents such as Claude Code or Gemini CLI. Describe what you want in plain language; the agent composes the tool's operations and brings back the result — a guided walk through a site in the conversation, or a dataset as a file.

## Installation

websurf is a command-line application, so install it with [pipx](https://pipx.pypa.io/), which puts it in its own isolated environment and on your PATH:

```bash
pipx install websurf
```

## Optional setup

1. **Chromium**

Install chromium (~190 MB) via playwright. This is needed for sites that require a browser render. If you skip it, websurf tells you to run this command the first time a page needs rendering.

```bash
websurf install-deps
```


2. **Claude skill**

Install a skill so that Claude can auto-discover websurf.

```bash
websurf skill --install
```

## The two jobs it does

**Exploration** — helping agents answer navigational questions about a site. What sections exist, what a page links to, what a section contains, what a page says. Agents move through the site step by step and the answers land in the chat. Nothing accumulates: pages pass through a self-cleaning cache, and that's all.

**Extraction** — turning repetitive pages into a dataset. When many pages share a layout (listings, catalogs, directories, indexes, profiles), the tool collaborates with your AI agent to work out the extraction pattern once and applies it across all of them with plain code, returning uniform records — one per item, each tagged with the page it came from.

The two compose: exploration that turns into "export all of that" continues from the same cache, with nothing fetched twice.

## When it fits

Use it when:
- You want to **explore** a website by talking to your agent.
- You want to extract a **structured list** out of one or more web pages — the same fields repeated across many items.
- The scale is **moderate** — a handful to a few dozen sites, hundreds to low thousands of pages.

It's a poor fit when:
- You need one known page read once — a plain page fetch is simpler.
- You need **very large scale, fast**. This is built for careful, moderate-volume work, not high-throughput scraping.

## Why not just fetch pages?

AI agents can already fetch and summarize pages. websurf does things slightly differently:

- **Structure vs content.** It returns page outlines, links, and main text with the boilerplate stripped.
- **JavaScript-built pages work.** Pages that come back empty from a plain fetch are detected and classified, and a real browser render can be used to fetch them.
- **Extracting information.** The moment you want to turn content on a website into a structured list or spreadsheet, websurf automatically extracts the data in a token-efficient manner.
- **Token efficiency.** Instead of passing each web page to the model (which can be costly), websurf uses the model to examine one or two sample pages and describe how to pull fields out. Based on this, it creates a small declarative program that is applied to every remaining page by ordinary code, with no further model involvement. Cost scales with the number of distinct page *layouts*, not the number of pages; extracting from ten pages or a thousand costs about the same. Frequently the model isn't needed at all — many sites already publish their data in machine-readable form inside the page (structured-data annotations, embedded JSON, meta tags), and the tool takes that path first.

## What you get back

Exploration: answers in the conversation.

Extraction: a uniform list of records — one per item, each carrying the page it came from. Load it into a spreadsheet or database, or hand it back to your AI agent for filtering, ranking, or deduplication.

## How to ask for it

Just describe the task to your agent. For exploration, name the site or page and what you want to know. For a dataset, include:

- **The starting addresses** — the listing or index pages to work from.
- **The fields you want** — e.g. name, description, price, location, link.
- **Any scope limits** — which subset counts, what to skip.

The agent picks the cheapest route to the data, says what it's doing, and reports anything it couldn't reach.

## Technical design

The organizing idea: **the language model writes the extraction program; deterministic code runs it.** The model is consulted only for the one thing it's uniquely good at — reading an unfamiliar page and describing how to get fields out of it. Everything else is ordinary code. That's why cost scales with layouts, not pages.

**The pipeline is a set of small primitives**, each doing one thing, composed by the agent (or by the autonomous `run` command):

- **`probe`** fetches a page and returns a compact *outline* instead of raw HTML: title, headings, any machine-readable data the site already publishes (JSON-LD, microdata, embedded JSON "data islands"), the page's link groups with counts, and a repeat-block analysis that flags whether the items of interest are already on this page. Decisions get made from this — cheaply, without a model call.
- **`expand`** lists every link in one of those groups, turning a listing page into a concrete set of URLs.
- **`sitemap`** lists the URL patterns a site publishes in its sitemaps — the link source for JavaScript-built listings whose static HTML carries no links at all.
- **`fetch`** pulls pages into the local cache, handling the messy parts: JavaScript-only pages (a headless browser render, but only when the page's data isn't already embedded in it), pages that are really error pages, cookie walls, redirect canonicalization. A render breaker guards sweeps over dead URLs: SPAs often serve HTTP 200 plus a not-found shell for deleted entities, so once most of a domain's renders classify as soft-404 the tool stops auto-rendering that domain and says so loudly rather than burning minutes of browser time on pages that no longer exist (`--no-render-breaker` overrides).
- **`text`** returns the readable main text of a cached page.
- **`synthesize`** is the only primitive that calls the model. Given one or two sample pages and the target fields, it returns an *extractor spec* — a declarative description (selectors, structured-data hooks, or a path into the page's embedded JSON) of how to get each field. It prefers data the site already publishes.
- **`extract`** applies a spec deterministically to any number of pages and emits records, streaming JSONL as it goes with per-page progress on stderr. No model involved, and no rendering either — island and meta specs read the un-rendered HTML, so extraction cost never silently escalates to a browser; a spec that needs rendered DOM gets it via an explicit `fetch --playwright` first.

**The autonomous `run` command** sits on top of the primitives: it probes the start pages, asks the model for a navigation-and-extraction plan, executes it over a few plan→execute cycles, and merges everything into one dataset. It adds planning-model calls on top of the per-layout synthesis cost, so it's the fallback for when the structure isn't visible up front — not the default. When the agent can see the layout itself, driving the primitives directly is cheaper.

**The model backend auto-selects:** a direct API client if a key is present, otherwise the local `claude` CLI on existing session auth. Extraction records are plain JSONL with provenance fields (`_source`, `_extractor`, `_run`) and deliberately schema-agnostic — mapping them into a downstream store is a separate, explicit step, never something the tool assumes.

**Agent discovery is designed in:** the critical pointers sit in the first lines of `--help` (agents habitually truncate help output), a one-line hint appears on stderr until the Claude Code skill is installed, and `websurf skill --install` writes the full driving guide where Claude Code discovers it automatically.

**Storage** is a single SQLite file per `--db` path, holding four things: the page cache, run history, extracted records, and synthesized extractor specs. Specs and pages are both cached, so re-running a job is near-instant and re-extracting is free — the model is paid again only when a layout is new. By default that file is a **cache, and treated like one**: it lives in the OS user cache directory (`~/Library/Caches/websurf/cache.db` on macOS, `~/.cache/websurf/` on Linux), pages older than the freshness window are pruned automatically, and deleting it is always safe — exploratory use accumulates nothing you have to manage. For a dataset you intend to keep, point the job at its own file in your project (`--db ./project.db`): explicit stores are durable, never auto-pruned, and keep a project's cache and output self-contained. Every step of a job must use the same store to share its cache. Read-only commands (`status`, `runs`, `export`) never create a store, and `prune` reclaims space in any store.
