"""Thin LLM wrapper: one provider (xAI, OpenAI-compatible), structured JSON output,
usage/cost logging and a record/replay cache.

Every call goes through :meth:`LLM.json`. Calls are keyed by a hash of
(model, system prompt, user prompt, schema), so the same request always maps to
the same fixture file under ``tests/fixtures/llm/``:

* ``replay``  - fixtures only; a missing fixture is an error. What CI and pytest use.
* ``auto``    - replay when a fixture exists, otherwise call the API and record it.
* ``record``  - always call the API and (re)write the fixture.
* ``live``    - always call the API, never write fixtures.

Each call (live or replayed) appends one row to the usage log with tokens,
latency and $ cost. The API key is read from the environment (or a ``.env``
file) at call time and is never logged, stored in fixtures or printed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4.7"
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
DEFAULT_USAGE = Path("out") / "usage.csv"
MODES = ("replay", "auto", "record", "live")

# $ per 1M tokens (input, cached input, output) below the 200k-token prompt threshold;
# requests at/over the threshold cost 2x. Source: docs.x.ai/docs/models (Oct 2026).
PRICES = {
    "grok-4.7": (2.00, 0.50, 6.00),
    "grok-4.6": (2.00, 0.50, 6.00),
    "grok-4.5": (2.00, 0.30, 6.00),
    "grok-4.3": (1.25, 0.20, 2.50),
    "grok-4.20-0309-reasoning": (1.25, 0.20, 2.50),
    "grok-4.20-0309-non-reasoning": (1.25, 0.20, 2.50),
}
LONG_CONTEXT = 200_000

USAGE_FIELDS = ["ts", "run_id", "task", "model", "source", "prompt_tokens", "cached_tokens",
                "completion_tokens", "reasoning_tokens", "latency_s", "cost_usd"]


class MissingFixture(RuntimeError):
    """No recorded response for this request, and no way to make a live call."""


@dataclass
class Usage:
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None   # exact, when the provider reports it


def estimate_cost(model: str, u: Usage) -> float:
    """Token-based cost. Reasoning tokens are billed at the output rate."""
    inp, cached, out = PRICES.get(model, PRICES[DEFAULT_MODEL])
    mult = 2.0 if u.prompt_tokens >= LONG_CONTEXT else 1.0
    fresh = max(u.prompt_tokens - u.cached_tokens, 0)
    return mult * (fresh * inp + u.cached_tokens * cached
                   + (u.completion_tokens + u.reasoning_tokens) * out) / 1e6


def find_api_key() -> str | None:
    """XAI_API_KEY from the environment, else from a .env file. Never printed."""
    key = os.environ.get("XAI_API_KEY")
    if key:
        return key.strip()
    for p in (Path.cwd() / ".env", ROOT / ".env", Path("/workspace/.env"),
              Path.home() / ".env"):
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if line.startswith("XAI_API_KEY="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        except OSError:
            continue
    return None


# A transport sends one request and returns (content, Usage). Swappable for tests.
Transport = Callable[[str, list[dict], dict], tuple[str, Usage]]


def openai_transport(api_key: str, base_url: str = BASE_URL) -> Transport:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=600)

    def send(model: str, messages: list[dict], response_format: dict) -> tuple[str, Usage]:
        r = client.chat.completions.create(model=model, messages=messages,
                                           response_format=response_format)
        u = r.usage
        ptd = getattr(u, "prompt_tokens_details", None)
        ctd = getattr(u, "completion_tokens_details", None)
        ticks = getattr(u, "cost_in_usd_ticks", None)
        if ticks is None and getattr(u, "model_extra", None):
            ticks = u.model_extra.get("cost_in_usd_ticks")
        usage = Usage(
            prompt_tokens=u.prompt_tokens or 0,
            cached_tokens=(getattr(ptd, "cached_tokens", 0) or 0) if ptd else 0,
            completion_tokens=u.completion_tokens or 0,
            reasoning_tokens=(getattr(ctd, "reasoning_tokens", 0) or 0) if ctd else 0,
            cost_usd=ticks / 1e10 if ticks is not None else None,
        )
        return r.choices[0].message.content, usage

    return send


class LLM:
    def __init__(self, model: str | None = None, mode: str | None = None,
                 fixtures_dir: Path = FIXTURES, usage_path: Path | None = DEFAULT_USAGE,
                 transport: Transport | None = None, run_id: str | None = None):
        self.model = model or os.environ.get("ONBOARD_MODEL", DEFAULT_MODEL)
        self.mode = mode or os.environ.get("ONBOARD_LLM_MODE", "auto")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.fixtures_dir = Path(fixtures_dir)
        self.usage_path = Path(usage_path) if usage_path else None
        self._transport = transport
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.calls: list[dict] = []

    # ---- helpers ---------------------------------------------------------
    def request_key(self, system: str, user: str, schema: dict) -> str:
        blob = json.dumps({"model": self.model, "system": system, "user": user,
                           "schema": schema}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def fixture_path(self, task: str, key: str) -> Path:
        return self.fixtures_dir / f"{task}-{key}.json"

    def has_fixture(self, task: str, system: str, user: str, schema: dict) -> bool:
        return self.fixture_path(task, self.request_key(system, user, schema)).exists()

    def _get_transport(self) -> Transport:
        if self._transport is None:
            key = find_api_key()
            if not key:
                raise MissingFixture("no recorded fixture for this request and XAI_API_KEY "
                                     "is not set; record fixtures with a key first")
            self._transport = openai_transport(key)
        return self._transport

    # ---- the one entry point ----------------------------------------------
    def json(self, task: str, system: str, user: str, schema: dict,
             schema_name: str = "result") -> dict:
        key = self.request_key(system, user, schema)
        path = self.fixture_path(task, key)
        if self.mode in ("replay", "auto") and path.exists():
            rec = json.loads(path.read_text())
            source = "replay"
        elif self.mode == "replay":
            raise MissingFixture(f"no fixture {path.name} (replay mode)")
        else:
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": user}]
            fmt = {"type": "json_schema",
                   "json_schema": {"name": schema_name, "schema": schema, "strict": True}}
            send = self._get_transport()
            t0 = time.perf_counter()
            content, usage = send(self.model, messages, fmt)
            latency = time.perf_counter() - t0
            cost = usage.cost_usd if usage.cost_usd is not None else estimate_cost(
                self.model, usage)
            rec = {"task": task, "key": key, "model": self.model,
                   "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "latency_s": round(latency, 3),
                   "usage": {**asdict(usage), "cost_usd": round(cost, 6),
                             "cost_exact": usage.cost_usd is not None},
                   "messages": messages, "response_format": fmt, "content": content}
            source = "live"
            if self.mode != "live":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
        self._log(task, source, rec)
        return json.loads(rec["content"])

    def _log(self, task: str, source: str, rec: dict) -> None:
        u = rec["usage"]
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "run_id": self.run_id, "task": task, "model": rec["model"], "source": source,
               "prompt_tokens": u["prompt_tokens"], "cached_tokens": u["cached_tokens"],
               "completion_tokens": u["completion_tokens"],
               "reasoning_tokens": u["reasoning_tokens"],
               "latency_s": rec["latency_s"], "cost_usd": u["cost_usd"]}
        self.calls.append(row)
        if self.usage_path is None:
            return
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.usage_path.exists()
        with self.usage_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=USAGE_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)

    def totals(self) -> dict:
        keys = ["prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens"]
        t = {k: sum(int(c[k]) for c in self.calls) for k in keys}
        t["calls"] = len(self.calls)
        t["cost_usd"] = round(sum(float(c["cost_usd"]) for c in self.calls), 6)
        t["latency_s"] = round(sum(float(c["latency_s"]) for c in self.calls), 2)
        return t
