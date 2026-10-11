"""LLM wrapper: record/replay, usage logging, cost, and key hygiene (all offline)."""

import csv
import json

import pytest

from onboard.llm import LLM, MissingFixture, Usage, estimate_cost

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["x"],
          "properties": {"x": {"type": "integer"}}}


class Echo:
    def __init__(self, usage=Usage(1200, 200, 50, 300)):
        self.n = 0
        self.usage = usage

    def __call__(self, model, messages, fmt):
        self.n += 1
        return json.dumps({"x": len(messages[1]["content"])}), self.usage


def test_record_then_replay(tmp_path):
    t = Echo()
    usage = tmp_path / "usage.csv"
    rec = LLM(mode="record", fixtures_dir=tmp_path / "fx", usage_path=usage, transport=t)
    assert rec.json("demo", "sys", "hello", SCHEMA) == {"x": 5}
    assert t.n == 1
    files = list((tmp_path / "fx").glob("demo-*.json"))
    assert len(files) == 1
    rep = LLM(mode="replay", fixtures_dir=tmp_path / "fx", usage_path=usage, transport=t)
    assert rep.json("demo", "sys", "hello", SCHEMA) == {"x": 5}
    assert t.n == 1  # replay never calls the transport
    rows = list(csv.DictReader(usage.open()))
    assert [r["source"] for r in rows] == ["live", "replay"]
    assert rows[0]["prompt_tokens"] == "1200" and rows[0]["reasoning_tokens"] == "300"
    assert float(rows[1]["cost_usd"]) == pytest.approx(float(rows[0]["cost_usd"]))


def test_any_change_to_the_request_misses_the_cache(tmp_path):
    t = Echo()
    llm = LLM(mode="auto", fixtures_dir=tmp_path, usage_path=None, transport=t)
    llm.json("demo", "sys", "hello", SCHEMA)
    llm.json("demo", "sys", "hello", SCHEMA)
    assert t.n == 1
    llm.json("demo", "sys", "hello!", SCHEMA)
    llm.json("demo", "sys2", "hello", SCHEMA)
    assert t.n == 3
    other_model = LLM(model="grok-4.3", mode="auto", fixtures_dir=tmp_path, usage_path=None,
                      transport=t)
    other_model.json("demo", "sys", "hello", SCHEMA)
    assert t.n == 4


def test_replay_without_fixture_fails_loudly(tmp_path):
    llm = LLM(mode="replay", fixtures_dir=tmp_path, usage_path=None, transport=Echo())
    with pytest.raises(MissingFixture):
        llm.json("demo", "sys", "hello", SCHEMA)


def test_auto_without_key_or_fixture_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr("onboard.llm.find_api_key", lambda: None)
    llm = LLM(mode="auto", fixtures_dir=tmp_path, usage_path=None)
    with pytest.raises(MissingFixture):
        llm.json("demo", "sys", "hello", SCHEMA)


def test_key_never_reaches_fixtures_or_logs(tmp_path, monkeypatch):
    secret = "xai-THIS-IS-A-FAKE-KEY-123456"
    monkeypatch.setenv("XAI_API_KEY", secret)
    usage = tmp_path / "usage.csv"
    llm = LLM(mode="record", fixtures_dir=tmp_path / "fx", usage_path=usage, transport=Echo())
    llm.json("demo", "sys", "hello", SCHEMA)
    for f in [usage, *(tmp_path / "fx").glob("*.json")]:
        assert secret not in f.read_text()


def test_cost():
    u = Usage(prompt_tokens=10_000, cached_tokens=2_000, completion_tokens=1_000,
              reasoning_tokens=3_000)
    # 8k fresh x $2 + 2k cached x $0.50 + 4k output (incl. reasoning) x $6, per 1M
    assert estimate_cost("grok-4.7", u) == pytest.approx((8_000 * 2 + 2_000 * 0.5
                                                          + 4_000 * 6) / 1e6)
    long = Usage(prompt_tokens=250_000)
    assert estimate_cost("grok-4.7", long) == pytest.approx(2 * 250_000 * 2 / 1e6)


def test_exact_provider_cost_wins(tmp_path):
    t = Echo(usage=Usage(1000, 0, 10, 0, cost_usd=0.0123))
    llm = LLM(mode="live", fixtures_dir=tmp_path, usage_path=None, transport=t)
    llm.json("demo", "sys", "hello", SCHEMA)
    assert llm.totals()["cost_usd"] == pytest.approx(0.0123)
    assert not list(tmp_path.glob("*.json"))  # live mode records nothing
