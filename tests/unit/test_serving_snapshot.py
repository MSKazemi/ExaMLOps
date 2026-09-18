"""The serving snapshot compiles every model, versions itself, and never publishes a partial view.

Plan P4.2 / ADR 0127. The compiler is exercised against a fake MLflow REST client, so these tests
pin exactly the calls it makes: one paginated search (the 100-model page limit was a live bug in
every replica) and one model-version lookup per *new* version.
"""

from __future__ import annotations

import json

import pytest

from examlops import serving_snapshot as snap
from examlops.data import serving
from examlops.events import schemas
from examlops.platform_db import get_db, init_db


class _Resp:
    def __init__(self, body: dict, status: int = 200) -> None:
        self._body, self.status_code = body, status

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeMlflow:
    """Registered models across pages; each with Production (and some with Canary)."""

    def __init__(self, n: int = 150, page: int = 100, fail: bool = False) -> None:
        self.models = [f"m{i:03d}" for i in range(n)]
        self.page = page
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []
        self.production = {name: "1" for name in self.models}

    def get(self, url: str, params: dict | None = None) -> _Resp:
        params = params or {}
        self.calls.append((url.rsplit("/mlflow/", 1)[1], dict(params)))
        if self.fail:
            raise ConnectionError("MLflow is down")
        if url.endswith("registered-models/search"):
            start = int(params.get("page_token") or 0)
            chunk = self.models[start : start + self.page]
            body: dict = {
                "registered_models": [
                    {
                        "name": name,
                        "aliases": [{"alias": "Production", "version": self.production[name]}]
                        + ([{"alias": "Canary", "version": "2"}] if name == "m000" else []),
                    }
                    for name in chunk
                ]
            }
            if start + self.page < len(self.models):
                body["next_page_token"] = str(start + self.page)
            return _Resp(body)
        name, version = params["name"], params["version"]
        return _Resp(
            {
                "model_version": {
                    "name": name,
                    "version": version,
                    "run_id": f"run-{name}-{version}",
                    "source": f"s3://mlflow/{name}/{version}",
                    "tags": [{"key": "framework", "value": "PyTorch"}] if name == "m001" else [],
                }
            }
        )


@pytest.fixture(autouse=True)
def _fresh_version_cache():
    snap._version_cache.clear()
    yield
    snap._version_cache.clear()


def _compile(fake: _FakeMlflow) -> dict:
    return snap.compile_snapshot(client=fake, mlflow_url="http://mlflow")


def test_every_page_of_the_registry_is_compiled():
    """150 models used to be 100: `search_registered_models()` read one page and stopped."""
    s = _compile(_FakeMlflow(n=150))

    assert len(s["models"]) == 150
    assert s["models"]["m000"]["aliases"]["Canary"]["version"] == "2"
    assert s["models"]["m001"]["aliases"]["Production"]["framework"] == "pytorch"
    assert s["models"]["m149"]["aliases"]["Production"]["source"] == "s3://mlflow/m149/1"


def test_a_version_is_looked_up_once_for_the_life_of_the_process():
    fake = _FakeMlflow(n=3)
    _compile(fake)
    fake.calls.clear()

    fake.production["m002"] = "5"  # one promotion
    _compile(fake)

    lookups = [c for c in fake.calls if c[0] == "model-versions/get"]
    assert lookups == [("model-versions/get", {"name": "m002", "version": "5"})]


def test_serving_config_rides_in_the_snapshot():
    serving.set_traffic_rules("M000", {"Production": 90, "Canary": 10}, updated_by="a")
    serving.set_shadow_config("M001", shadow_alias="Staging", enabled=True, updated_by="a")
    serving.set_shadow_config("M002", shadow_alias="Staging", enabled=True, updated_by="a")
    serving.set_shadow_config("M002", enabled=False, updated_by="a")

    s = _compile(_FakeMlflow(n=3))

    assert s["traffic"] == {"m000": {"model": "M000", "rules": {"Production": 90, "Canary": 10}}}
    assert s["shadow"] == {"m001": {"model": "M001", "alias": "Staging"}}


def test_an_unreadable_mlflow_fails_the_compile_rather_than_emptying_it():
    """A partial snapshot would tell every replica to unload the models it could not see."""
    with pytest.raises(ConnectionError):
        _compile(_FakeMlflow(fail=True))


def test_the_digest_depends_on_content_not_key_order():
    a = {"models": {"x": {"aliases": {"P": 1, "C": 2}}}, "traffic": {}, "shadow": {}}
    b = {"shadow": {}, "traffic": {}, "models": {"x": {"aliases": {"C": 2, "P": 1}}}}
    assert snap.digest_of(a) == snap.digest_of(b)
    assert snap.digest_of(a) != snap.digest_of({**a, "traffic": {"x": {}}})


# ─── publishing ──────────────────────────────────────────────────────────────


def _events(topic: str) -> list[dict]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT payload FROM event_outbox WHERE topic=? ORDER BY id", (topic,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_a_generation_moves_only_when_the_content_does():
    fake = _FakeMlflow(n=2)

    first = snap.publish(_compile(fake))
    again = snap.publish(_compile(fake))
    fake.production["m001"] = "2"
    moved = snap.publish(_compile(fake))

    assert first == (1, True)
    assert again == (1, False)
    assert moved == (2, True)
    assert snap.latest()["generation"] == 2
    assert snap.latest()["models"]["m001"]["aliases"]["Production"]["version"] == "2"


def test_publishing_announces_the_generation_with_a_conforming_event():
    snap.publish(_compile(_FakeMlflow(n=2)))

    [event] = _events("serving.snapshot_published")
    assert event["generation"] == 1 and event["models"] == 2
    assert schemas.validate("serving.snapshot_published", event) == []


def test_old_generations_are_pruned(monkeypatch):
    monkeypatch.setattr(snap, "_KEEP", 3)
    fake = _FakeMlflow(n=1)
    for version in range(1, 7):
        fake.production["m000"] = str(version)
        snap.publish(_compile(fake))

    with get_db() as conn:
        kept = [r[0] for r in conn.execute("SELECT generation FROM serving_snapshots")]
    assert kept == [4, 5, 6]


def test_the_watermark_moves_only_for_serving_topics():
    from examlops.data.events import enqueue_event

    before = snap.trigger_watermark()
    enqueue_event("retrain.scheduled", {"model_name": "x", "dataset_name": "d", "flow_run_id": "f"})
    assert snap.trigger_watermark() == before

    serving.set_traffic_rules("M000", {"Production": 100}, updated_by="a")
    assert snap.trigger_watermark() > before


def test_publishing_mirrors_to_the_kv_bucket_when_the_backbone_is_nats(monkeypatch):
    from examlops.events import nats_backend

    stored: list[tuple[str, str, bytes]] = []

    class _KV:
        def kv_put(self, bucket, key, value):
            stored.append((bucket, key, value))
            return 1

    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "nats")
    monkeypatch.setattr(nats_backend, "shared", lambda: _KV())

    generation, _ = snap.publish(_compile(_FakeMlflow(n=1)))

    [(bucket, key, value)] = stored
    assert (bucket, key) == ("examlops-serving", "snapshot")
    assert json.loads(value)["generation"] == generation


def test_a_kv_outage_does_not_fail_the_publish(monkeypatch):
    from examlops.events import nats_backend

    def _down():
        raise ConnectionError("nats down")

    monkeypatch.setenv("EXAMLOPS_EVENT_PUBLISHER", "nats")
    monkeypatch.setattr(nats_backend, "shared", _down)

    assert snap.publish(_compile(_FakeMlflow(n=1))) == (1, True)


# ─── exa serve snapshot ──────────────────────────────────────────────────────


def test_the_cli_says_when_no_snapshot_exists():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    result = CliRunner().invoke(app, ["serve", "snapshot", "show"])
    assert result.exit_code == 0
    assert "No serving snapshot" in result.output


def test_the_cli_publishes_and_shows_the_generation(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    fake = _FakeMlflow(n=2)
    real = snap.compile_snapshot
    monkeypatch.setattr(
        snap, "compile_snapshot", lambda: real(client=fake, mlflow_url="http://mlflow")
    )
    runner = CliRunner()

    published = runner.invoke(app, ["--json", "serve", "snapshot", "publish"])
    shown = runner.invoke(app, ["--json", "serve", "snapshot", "show"])

    assert published.exit_code == 0, published.output
    assert json.loads(published.output) == {"generation": 1, "published": True}
    body = json.loads(shown.output)
    assert body["generation"] == 1 and set(body["models"]) == {"m000", "m001"}
    with get_db() as conn:
        audited = conn.execute(
            "SELECT count(*) FROM audit_events WHERE action='serving_snapshot_published'"
        ).fetchone()[0]
    assert audited == 1
