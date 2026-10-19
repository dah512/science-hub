"""Science Hub API tests. The Anthropic client is replaced with a fake, so no key or network is used."""
from types import SimpleNamespace

import anthropic
import pytest
from fastapi.testclient import TestClient

from app import main


class FakeMessages:
    def __init__(self):
        self.calls = []
        self.raise_exc = None

    def create(self, **kw):
        self.calls.append(kw)
        if self.raise_exc:
            raise self.raise_exc
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Great question! What do you think happens?")])


@pytest.fixture()
def fake(monkeypatch):
    msgs = FakeMessages()
    monkeypatch.setattr(main, "_client", SimpleNamespace(messages=msgs))
    monkeypatch.setattr(main, "limiter", main.Limiter())
    return msgs


@pytest.fixture()
def client():
    return TestClient(main.app)


def q(text="Why is the sky blue?", **kw):
    return {"subject": "physics", "level": "middle", "mode": "guide",
            "messages": [{"role": "user", "content": text}], **kw}


def test_health(client, fake):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["ready"]
    assert "physics" in r.json()["subjects"]


def test_ask_returns_reply_and_sends_safety_prompt(client, fake):
    r = client.post("/api/ask", json=q())
    assert r.status_code == 200
    assert r.json()["reply"].startswith("Great question")
    call = fake.calls[0]
    assert call["model"] == main.MODEL
    assert "988" in call["system"] and "Physics" in call["system"] and "GUIDE MODE" in call["system"]
    assert r.headers["cache-control"] == "no-store"


def test_explain_mode_and_level(client, fake):
    client.post("/api/ask", json=q(mode="explain", level="high"))
    s = fake.calls[0]["system"]
    assert "EXPLAIN MODE" in s and "high school" in s


def test_personal_details_are_removed_before_sending(client, fake):
    r = client.post("/api/ask", json=q("I'm at 3910 Lockwood Avenue, call 419-555-0100 or email kid@example.com"))
    assert r.json()["redacted"] is True
    sent = fake.calls[0]["messages"][0]["content"]
    assert "419" not in sent and "example.com" not in sent and "Lockwood" not in sent


@pytest.mark.parametrize("bad", [
    {"subject": "alchemy"},
    {"level": "college"},
    {"mode": "cheat"},
])
def test_rejects_unknown_options(client, fake, bad):
    assert client.post("/api/ask", json=q(**bad)).status_code == 422
    assert fake.calls == []


def test_rejects_bad_conversation_shape(client, fake):
    two_users = q()
    two_users["messages"] = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    assert client.post("/api/ask", json=two_users).status_code == 422
    ends_assistant = q()
    ends_assistant["messages"] = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert client.post("/api/ask", json=ends_assistant).status_code == 422
    too_long = q("x" * (main.MAX_CHARS + 1))
    assert client.post("/api/ask", json=too_long).status_code == 422
    assert fake.calls == []


def test_hourly_limit_per_client(client, fake, monkeypatch):
    monkeypatch.setattr(main, "PER_CLIENT_PER_HOUR", 2)
    h = {"x-forwarded-for": "203.0.113.5"}
    assert client.post("/api/ask", json=q(), headers=h).status_code == 200
    assert client.post("/api/ask", json=q(), headers=h).status_code == 200
    assert client.post("/api/ask", json=q(), headers=h).status_code == 429
    # a different student is unaffected
    assert client.post("/api/ask", json=q(), headers={"x-forwarded-for": "198.51.100.7"}).status_code == 200


def test_daily_cap(client, fake, monkeypatch):
    monkeypatch.setattr(main, "DAILY_CAP", 1)
    assert client.post("/api/ask", json=q()).status_code == 200
    r = client.post("/api/ask", json=q(), headers={"x-forwarded-for": "198.51.100.9"})
    assert r.status_code == 429 and "tomorrow" in r.json()["error"]


def test_no_key_gives_503_and_still_starts(client, monkeypatch):
    monkeypatch.setattr(main, "_client", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.get("/api/health").json()["ready"] is False
    assert client.post("/api/ask", json=q()).status_code == 503


def test_upstream_error_is_friendly(client, fake):
    req = SimpleNamespace(method="POST", url="https://api.anthropic.com", headers={})
    fake.raise_exc = anthropic.APIConnectionError(request=req)
    r = client.post("/api/ask", json=q())
    assert r.status_code == 502 and "try again" in r.json()["error"]
