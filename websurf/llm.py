"""LLM adapter + the meter.

The meter is the enforcement currency: measured tokens/fetches/renders with
hard budgets. Counts (in plans) are the planning currency; when counts lie —
and they do — the meter is the backstop.

Backends:
- api:  official `anthropic` SDK (needs ANTHROPIC_API_KEY)
- cli:  `claude -p --output-format json` subprocess (runs on the user's
        Claude Code auth; no API key needed)
- any object with .ask(prompt, system, max_tokens) -> (text, in_tok, out_tok)
  works (tests inject a fake).
"""
import json
import os
import re
import shutil
import subprocess


class BudgetExceeded(RuntimeError):
    pass


class Meter:
    def __init__(self, config):
        self.config = config
        self.tokens_in = 0
        self.tokens_out = 0
        self.tokens_by_purpose = {}
        self.llm_calls = 0
        self.fetches = 0
        self.renders = 0
        self.cache_hits = 0

    @property
    def tokens(self):
        return self.tokens_in + self.tokens_out

    def check_tokens(self):
        if self.tokens >= self.config.budget_tokens:
            raise BudgetExceeded(
                f"token budget exhausted ({self.tokens}/{self.config.budget_tokens})")

    def add_tokens(self, purpose, tin, tout):
        self.tokens_in += tin
        self.tokens_out += tout
        self.llm_calls += 1
        p = self.tokens_by_purpose.setdefault(purpose, [0, 0])
        p[0] += tin
        p[1] += tout

    def count_fetch(self):
        if self.fetches >= self.config.budget_fetches:
            raise BudgetExceeded(f"fetch budget exhausted ({self.fetches})")
        self.fetches += 1

    def renders_available(self):
        return self.renders < self.config.budget_renders

    def count_render(self):
        if not self.renders_available():
            raise BudgetExceeded(f"render budget exhausted ({self.renders})")
        self.renders += 1

    def summary(self):
        return {
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_by_purpose": {k: {"in": v[0], "out": v[1]}
                                  for k, v in self.tokens_by_purpose.items()},
            "fetches": self.fetches,
            "renders": self.renders,
            "cache_hits": self.cache_hits,
        }


def parse_json_loose(text):
    """Tolerate code fences and prose around the JSON payload."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON object found in LLM reply")
    start = min(starts)
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    return obj


class LLM:
    def __init__(self, config, meter, backend=None):
        self.config = config
        self.meter = meter
        self.backend = backend or _pick_backend(config)

    def ask(self, prompt, system=None, max_tokens=4000, purpose="plan"):
        self.meter.check_tokens()
        text, tin, tout = self.backend.ask(prompt, system, max_tokens)
        self.meter.add_tokens(purpose, tin, tout)
        return text

    def ask_json(self, prompt, system=None, max_tokens=4000, purpose="plan"):
        text = self.ask(prompt, system, max_tokens, purpose)
        try:
            return parse_json_loose(text)
        except (ValueError, json.JSONDecodeError):
            retry = (prompt + "\n\nYour previous reply was not valid JSON. "
                     "Reply with ONLY the JSON object, no prose, no code fences.")
            text = self.ask(retry, system, max_tokens, purpose)
            return parse_json_loose(text)


class ApiBackend:
    def __init__(self, model):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model

    def ask(self, prompt, system, max_tokens):
        kwargs = {"model": self.model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]}
        if system:
            kwargs["system"] = system
        resp = self.client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise RuntimeError("model refused the request")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return text, resp.usage.input_tokens, resp.usage.output_tokens


class CliBackend:
    """Shells out to `claude -p` — runs on the user's Claude Code session auth."""

    def __init__(self, model):
        self.model = model

    def ask(self, prompt, system, max_tokens):
        full = f"<instructions>\n{system}\n</instructions>\n\n{prompt}" if system else prompt
        cmd = ["claude", "-p", "--output-format", "json", "--model", self.model]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        proc = subprocess.run(cmd, input=full, capture_output=True, text=True,
                              timeout=900, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {proc.stderr.strip()[:500]}")
        try:
            obj = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"claude CLI returned non-JSON: {proc.stdout[:300]}") from e
        if obj.get("is_error"):
            raise RuntimeError(f"claude CLI error: {obj.get('result', '')[:500]}")
        text = obj.get("result", "")
        usage = obj.get("usage") or {}
        tin = usage.get("input_tokens") or len(full) // 4
        tout = usage.get("output_tokens") or len(text) // 4
        return text, tin, tout


def _pick_backend(config):
    choice = config.backend
    if choice == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                return ApiBackend(config.model)
            except ImportError:
                pass
        if shutil.which("claude"):
            return CliBackend(config.model)
        raise RuntimeError(
            "no LLM backend: set ANTHROPIC_API_KEY (+ pip install anthropic) "
            "or install the claude CLI, or use the deterministic subcommands")
    if choice == "api":
        return ApiBackend(config.model)
    if choice == "cli":
        return CliBackend(config.model)
    raise RuntimeError(f"unknown backend {choice!r}")
