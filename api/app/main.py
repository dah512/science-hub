"""EDMHS Science Hub API: a youth STEM tutor backed by Claude.

Privacy design: no accounts, no stored conversations. The only state kept is
in-memory request counters keyed by a salted hash of the client address, which
reset when the instance restarts. Deploy with max-instances=1 so the daily cap
is exact.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Literal

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.prompt import LEVELS, MODES, SUBJECTS, build_system

VERSION = "1.0.0"
MODEL = os.getenv("TUTOR_MODEL", "claude-haiku-5-5")
MAX_TOKENS = int(os.getenv("TUTOR_MAX_TOKENS", "700"))
PER_CLIENT_PER_HOUR = int(os.getenv("PER_CLIENT_PER_HOUR", "20"))
DAILY_CAP = int(os.getenv("DAILY_CAP", "300"))
MAX_TURNS = 12
MAX_CHARS = 1500

log = logging.getLogger("science-hub")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

app = FastAPI(title="EDMHS Science Hub API", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic | None:
    """Created lazily so the container starts and listens even without a key."""
    global _client
    if _client is None:
        key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            return None
        _client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=45.0)
    return _client


# ---------------------------------------------------------------- limits
class Limiter:
    def __init__(self) -> None:
        self.salt = secrets.token_bytes(16)
        self.lock = threading.Lock()
        self.hits: dict[str, deque[float]] = {}
        self.day = ""
        self.day_count = 0

    def key(self, ip: str) -> str:
        return hashlib.sha256(self.salt + ip.encode()).hexdigest()[:16]

    def check(self, ip: str, now: float | None = None) -> str | None:
        now = now or time.time()
        today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        with self.lock:
            if today != self.day:
                self.day, self.day_count = today, 0
            if self.day_count >= DAILY_CAP:
                return "daily"
            q = self.hits.setdefault(self.key(ip), deque())
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= PER_CLIENT_PER_HOUR:
                return "hourly"
            q.append(now)
            self.day_count += 1
            if len(self.hits) > 5000:  # keep memory bounded
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 3600]:
                    del self.hits[k]
            return None


limiter = Limiter()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


# ---------------------------------------------------------------- privacy
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Blvd|Ln|Lane|Ct|Court|Way|Pl|Place)\b\.?"
)


def redact(text: str) -> tuple[str, bool]:
    out = _EMAIL.sub("[email removed]", text)
    out = _PHONE.sub("[phone removed]", out)
    out = _STREET.sub("[address removed]", out)
    return out, out != text


# ---------------------------------------------------------------- schema
class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class AskIn(BaseModel):
    subject: str
    level: str = "middle"
    mode: str = "guide"
    messages: list[Turn] = Field(min_length=1, max_length=MAX_TURNS)

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        if v not in SUBJECTS:
            raise ValueError("unknown subject")
        return v

    @field_validator("level")
    @classmethod
    def _level(cls, v: str) -> str:
        if v not in LEVELS:
            raise ValueError("unknown level")
        return v

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in MODES:
            raise ValueError("unknown mode")
        return v

    @field_validator("messages")
    @classmethod
    def _shape(cls, v: list[Turn]) -> list[Turn]:
        if v[0].role != "user" or v[-1].role != "user":
            raise ValueError("conversation must start and end with the student")
        for a, b in zip(v, v[1:]):
            if a.role == b.role:
                raise ValueError("turns must alternate")
        if len(v[-1].content) > MAX_CHARS:
            raise ValueError(f"question is longer than {MAX_CHARS} characters")
        return v


def err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- routes
@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION, "model": MODEL, "ready": get_client() is not None,
            "subjects": SUBJECTS}


@app.post("/api/ask")
def ask(body: AskIn, request: Request):
    client = get_client()
    if client is None:
        return err(503, "The tutor is not switched on yet. Please check back soon.")

    blocked = limiter.check(client_ip(request))
    if blocked == "daily":
        return err(429, "The Science Hub has answered all the questions it can for today. Come back tomorrow!")
    if blocked == "hourly":
        return err(429, "You've asked a lot of great questions this hour. Take a short break and try again later.")

    msgs, removed = [], False
    for t in body.messages:
        text = t.content
        if t.role == "user":
            text, r = redact(text)
            removed = removed or r
        msgs.append({"role": t.role, "content": text})

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=build_system(body.subject, body.level, body.mode),
            messages=msgs,
        )
    except anthropic.RateLimitError:
        return err(429, "Lots of students are asking right now. Try again in a minute.")
    except anthropic.APIError as e:  # includes auth, overload, bad request
        log.warning("anthropic error: %s", type(e).__name__)
        return err(502, "The tutor couldn't answer just now. Please try again in a moment.")

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if not text:
        text = "I'm not able to help with that one. Want to try a different science question?"
    # Log counts only: never the question or the answer.
    log.info("ask subject=%s level=%s mode=%s turns=%d redacted=%s", body.subject, body.level,
             body.mode, len(msgs), removed)
    return {"reply": text, "redacted": removed}


@app.exception_handler(ValueError)
async def bad_value(_: Request, exc: ValueError):
    return err(422, str(exc))
