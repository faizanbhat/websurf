"""LLM-guided crawler v2.

Thesis: the LLM writes and repairs the program; deterministic code runs it.
- Deterministic: fetch, render, canonicalize, project, apply extractors,
  health-check, dedup, meter, clamp, measure progress.
- LLM: interpret the objective, pick link groups from outlines, synthesize
  declarative extractors, extract from hostile/one-off pages, decide done.

See README.md (repo root) for the design -> implementation map.
"""
