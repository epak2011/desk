import worker


def test_claude_regime_context_uses_rules_fallback_without_key(monkeypatch):
    monkeypatch.setattr(worker, "_api_key", lambda: "")
    text, source = worker._claude_regime_context(snapshot={"score": 2}, fallback="Rules fallback.")
    assert text == "Rules fallback."
    assert source == "rules_fallback"


def test_claude_regime_context_accepts_natural_paragraph(monkeypatch):
    class Block:
        text = " ".join(["Market leadership remains uneven while the broader trend is still intact."] * 8)

    class Response:
        content = [Block()]

    monkeypatch.setattr(worker, "_api_key", lambda: "test-key")
    monkeypatch.setattr(worker, "_messages_create", lambda *args, **kwargs: Response())
    monkeypatch.setitem(__import__("sys").modules, "anthropic", type("M", (), {"Anthropic": lambda api_key: object()}))
    text, source = worker._claude_regime_context(snapshot={"score": 2}, fallback="Rules fallback.")
    assert text.startswith("Market leadership")
    assert source == "claude"


def test_claude_regime_context_rejects_bad_output(monkeypatch):
    class Block:
        text = "Too short."

    class Response:
        content = [Block()]

    monkeypatch.setattr(worker, "_api_key", lambda: "test-key")
    monkeypatch.setattr(worker, "_messages_create", lambda *args, **kwargs: Response())
    monkeypatch.setitem(__import__("sys").modules, "anthropic", type("M", (), {"Anthropic": lambda api_key: object()}))
    text, source = worker._claude_regime_context(snapshot={"score": 2}, fallback="Rules fallback.")
    assert text == "Rules fallback."
    assert source == "rules_fallback"
