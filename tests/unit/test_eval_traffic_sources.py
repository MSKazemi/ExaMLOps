"""ADR 0007 decision 2 — the live-traffic sampler's two sources.

`sample_by_request_hash` only *ordered* items a caller supplied; nothing pulled traffic. The
predictions source reads labelled predictions from platform_db; the Tempo source searches C1 GenAI
spans with TraceQL. The Tempo HTTP API is faked with its documented search-response shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.evaluation import sample_by_request_hash  # noqa: E402
from examlops.evaluation import traffic as tr  # noqa: E402

NOW = 1_780_000_000.0


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for name in (tr.TEMPO_URL_ENV, tr.TEMPO_TOKEN_ENV, tr.TEMPO_ORG_ENV, tr.TEMPO_TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)
    from examlops import platform_db

    platform_db.init_db()


def _pred(model, h, p, *, age=60, alias="Production"):
    from examlops import platform_db

    with platform_db.get_db() as conn:
        conn.execute(
            "INSERT INTO predictions (model, alias, request_hash, prediction, ts)"
            " VALUES (?,?,?,?,datetime(?, 'unixepoch'))",
            (model, alias, h, p, int(NOW) - age),
        )


def _label(h, y):
    from examlops import platform_db

    with platform_db.get_db() as conn:
        conn.execute("INSERT INTO ground_truth (request_hash, label) VALUES (?,?)", (h, y))


def _src():
    return tr.PredictionsSource(clock=lambda: NOW)


# -- predictions -------------------------------------------------------------------------------


def test_only_labelled_predictions_in_the_window_are_returned():
    _pred("JPCP", "a", 1.0)
    _label("a", 1.5)
    _pred("JPCP", "unlabelled", 2.0)
    _pred("JPCP", "old", 3.0, age=7200)
    _label("old", 3.0)
    _pred("OTHER", "b", 4.0)
    _label("b", 4.0)
    pull = _src().pull("JPCP", since_s=3600, limit=100)
    assert [i.request_hash for i in pull.items] == ["a"]
    item = pull.items[0]
    assert float(item.output) == 1.0 and float(item.reference) == 1.5


def test_the_newest_label_and_newest_prediction_per_hash_win():
    _pred("JPCP", "a", 1.0, age=120)
    _pred("JPCP", "a", 2.0, age=60)
    _label("a", 5.0)
    _label("a", 6.0)
    pull = _src().pull("JPCP", since_s=3600, limit=100)
    assert len(pull.items) == 1
    assert float(pull.items[0].output) == 2.0 and float(pull.items[0].reference) == 6.0


def test_alias_filter_and_limit_apply_in_sql():
    for i in range(10):
        _pred("JPCP", f"p{i}", float(i), age=100 - i)
        _label(f"p{i}", float(i))
    _pred("JPCP", "canary", 9.0, alias="Canary")
    _label("canary", 9.0)
    pull = _src().pull("JPCP", since_s=3600, limit=3, alias="Production")
    assert [i.request_hash for i in pull.items] == ["p9", "p8", "p7"]  # newest first, bounded


def test_limit_is_capped():
    assert tr._bounded(10**9) == tr.MAX_LIMIT
    assert tr._bounded(0) == tr.DEFAULT_LIMIT


def test_predictions_refuse_a_non_default_tenant():
    with pytest.raises(tr.TrafficSourceError, match="no tenant"):
        _src().pull("JPCP", since_s=60, limit=1, tenant="centre-b")


def test_the_sample_is_deterministic_over_recorded_hashes():
    for i in range(30):
        _pred("JPCP", f"h{i:02d}", float(i))
        _label(f"h{i:02d}", float(i))
    items = _src().pull("JPCP", since_s=3600, limit=100).items
    a = [i.request_hash for i in sample_by_request_hash(items, 5)]
    b = [i.request_hash for i in sample_by_request_hash(list(reversed(items)), 5)]
    assert a == b == sorted(f"h{i:02d}" for i in range(30))[:5]


# -- tempo ---------------------------------------------------------------------------------------


def _attr(k, v):
    return {"key": k, "value": {"stringValue": v}}


def _span(h, *, start, prompt=None, completion=None, alias=None, messages=False):
    attrs = [_attr("examlops.request_hash", h)]
    if messages:
        if prompt is not None:
            attrs.append(
                _attr(
                    "gen_ai.input.messages",
                    json.dumps([{"role": "user", "parts": [{"type": "text", "content": prompt}]}]),
                )
            )
        if completion is not None:
            attrs.append(
                _attr(
                    "gen_ai.output.messages",
                    json.dumps(
                        [{"role": "assistant", "parts": [{"type": "text", "content": completion}]}]
                    ),
                )
            )
    else:
        if prompt is not None:
            attrs.append(_attr("gen_ai.prompt", prompt))
        if completion is not None:
            attrs.append(_attr("gen_ai.completion", completion))
    if alias:
        attrs.append(_attr("examlops.model.alias", alias))
    return {"spanID": h, "startTimeUnixNano": str(start), "attributes": attrs}


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


class FakeGet:
    def __init__(self, body, status=200):
        self.body, self.status, self.calls = body, status, []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        return FakeResp(self.body, self.status)


def _tempo(body, status=200):
    get = FakeGet(body, status)
    return tr.TempoSource(base_url="http://tempo:3200/", http_get=get, clock=lambda: NOW), get


def test_tempo_query_filters_model_and_tenant_server_side_and_is_bounded(monkeypatch):
    monkeypatch.setenv(tr.TEMPO_TOKEN_ENV, "t0k")
    monkeypatch.setenv(tr.TEMPO_ORG_ENV, "centre-a")
    src, get = _tempo({"traces": []})
    pull = src.pull("chat", since_s=600, limit=10**6, tenant="centre-a")
    url, kw = get.calls[0]
    assert url == "http://tempo:3200/api/search"
    q = kw["params"]["q"]
    assert 'span.gen_ai.request.model = "chat"' in q and 'span.examlops.tenant = "centre-a"' in q
    assert "span.examlops.request_hash != nil" in q
    assert kw["params"]["limit"] == str(tr.MAX_LIMIT)
    assert kw["params"]["end"] == str(int(NOW)) and kw["params"]["start"] == str(int(NOW) - 600)
    assert kw["headers"]["Authorization"] == "Bearer t0k"
    assert kw["headers"]["X-Scope-OrgID"] == "centre-a"
    assert kw["timeout"] == tr.DEFAULT_TEMPO_TIMEOUT_S
    assert pull.items == [] and pull.note == "no matching spans in the window"


def test_tempo_parses_both_content_shapes_dedupes_and_keeps_newest():
    body = {
        "traces": [
            {
                "traceID": "t1",
                "spanSets": [
                    {
                        "spans": [
                            _span("h1", start=100, prompt="old q", completion="old a"),
                            _span("h2", start=300, prompt="q2", completion="a2", messages=True),
                        ]
                    }
                ],
            },
            {
                "traceID": "t2",
                "spanSet": {
                    "spans": [
                        _span("h1", start=200, prompt="new q", completion="new a"),
                    ]
                },
            },
        ]
    }
    src, _ = _tempo(body)
    pull = src.pull("chat", since_s=600, limit=10)
    by = {i.request_hash: i for i in pull.items}
    assert set(by) == {"h1", "h2"}
    assert by["h1"].output == "new a" and by["h1"].prompt == "new q"
    assert by["h2"].output == "a2" and by["h2"].prompt == "q2"
    assert pull.duplicates == 1 and pull.seen == 3


def test_spans_without_captured_content_are_counted_and_explained():
    body = {"traces": [{"spanSets": [{"spans": [_span("h1", start=1), _span("h2", start=2)]}]}]}
    src, _ = _tempo(body)
    pull = src.pull("chat", since_s=600, limit=10)
    assert pull.items == [] and pull.no_content == 2
    assert "EXAMLOPS_GENAI_CAPTURE_CONTENT" in pull.note


def test_alias_mismatch_is_dropped_but_unlabelled_alias_is_kept():
    body = {
        "traces": [
            {
                "spanSets": [
                    {
                        "spans": [
                            _span("h1", start=1, completion="a", alias="Canary"),
                            _span("h2", start=2, completion="b", alias="Production"),
                            _span("h3", start=3, completion="c"),
                        ]
                    }
                ]
            }
        ]
    }
    src, _ = _tempo(body)
    pull = src.pull("chat", since_s=600, limit=10, alias="Production")
    assert sorted(i.request_hash for i in pull.items) == ["h2", "h3"]


@pytest.mark.parametrize("model", ['chat" || true', "a b", "", "x}" + "y"])
def test_names_that_could_escape_traceql_are_refused(model):
    src, get = _tempo({"traces": []})
    with pytest.raises(tr.TrafficSourceError, match="not a valid name"):
        src.pull(model, since_s=60, limit=1)
    assert get.calls == []  # refused before any request


def test_http_errors_and_unreachable_tempo_are_source_errors():
    src, _ = _tempo({}, status=503)
    with pytest.raises(tr.TrafficSourceError, match="HTTP 503"):
        src.pull("chat", since_s=60, limit=1)

    def boom(*a, **k):
        raise ConnectionError("refused")

    down = tr.TempoSource(base_url="http://tempo:3200", http_get=boom)
    with pytest.raises(tr.TrafficSourceError, match="ConnectionError"):
        down.pull("chat", since_s=60, limit=1)


def test_tempo_needs_an_http_url():
    with pytest.raises(tr.TrafficSourceError, match=tr.TEMPO_URL_ENV):
        tr.TempoSource().pull("chat", since_s=60, limit=1)
    with pytest.raises(tr.TrafficSourceError, match="http"):
        tr.TempoSource(base_url="file:///etc/passwd").pull("chat", since_s=60, limit=1)


def test_unknown_source_is_refused():
    with pytest.raises(tr.TrafficSourceError, match="unknown traffic source"):
        tr.get_source("kafka")
