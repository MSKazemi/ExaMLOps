from skipper import confirm


def _make_tool():
    @confirm.confirmed_write(lambda x: f"do {x}")
    def risky(x: str) -> str:
        return f"did {x}"

    return risky


def test_registers_write_tool():
    _make_tool()
    assert "risky" in confirm.WRITE_TOOLS


def test_proceeds_on_yes(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "yes")
    risky = _make_tool()
    assert risky(x="A") == "did A"


def test_cancels_on_no(monkeypatch):
    monkeypatch.setattr(confirm, "interrupt", lambda payload: "no")
    risky = _make_tool()
    assert risky(x="A") == "Cancelled — no action taken."


def test_interrupt_payload_shape(monkeypatch):
    captured = {}

    def fake_interrupt(payload):
        captured.update(payload)
        return True

    monkeypatch.setattr(confirm, "interrupt", fake_interrupt)
    risky = _make_tool()
    risky(x="Z")
    assert captured["action"] == "risky"
    assert captured["args"] == {"x": "Z"}
    assert captured["summary"] == "do Z"


def test_affirmative_parsing():
    assert confirm._is_affirmative("y")
    assert confirm._is_affirmative(True)
    assert not confirm._is_affirmative("n")
    assert not confirm._is_affirmative("")
