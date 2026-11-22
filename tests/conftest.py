from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from munshig.config import Settings
from munshig.db import DB
from munshig.llm import ScriptedLLM
from munshig.parties import refresh_ledger_cache
from munshig.service import Service
from munshig.tally.client import TallyClient
from munshig.tally.mock import MockTally
from munshig.whatsapp import FakeChannel

SAMPLES = Path(__file__).resolve().parents[1] / "munshig" / "demo" / "samples"
OUR_GSTIN = "27AABCD1234E1Z8"
OWNER = "919800000001"


@pytest.fixture
def truth() -> dict:
    return json.loads((SAMPLES / "truth.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(home=tmp_path)
    s.business.name, s.business.gstin = "Demo Traders", OUR_GSTIN
    s.owner.numbers = [OWNER]
    s.tally.company = "Demo Traders"
    return s


@pytest.fixture
def db() -> DB:
    return DB(":memory:")


@pytest.fixture
def mock() -> MockTally:
    return MockTally(company="Demo Traders")


@pytest.fixture
def tally(mock, db) -> TallyClient:
    client = TallyClient(company="Demo Traders", transport=mock.transport())
    refresh_ledger_cache(db, client.ledgers())
    return client


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 21, 11, 0))


@pytest.fixture
def make_service(settings, db, tally, clock):
    def factory(llm=None, mode="draft"):
        settings.assistant.mode = mode
        channel = FakeChannel()
        svc = Service(settings, db, channel, tally, llm, clock=clock, sleep=lambda s: None,
                      background_extraction=False)
        svc.me = "919800000099@s.whatsapp.net"
        return svc, channel
    return factory


def reply_llm(answers: dict[str, dict], default: dict | None = None) -> ScriptedLLM:
    """A scripted model: the first key found in the customer's text picks the answer."""
    def answer(system, prompt, vision):
        incoming = prompt.split("New message(s) from the customer:", 1)[-1].lower()
        for key, value in answers.items():
            if key in incoming:
                return json.dumps(value)
        return json.dumps(default or {"can_answer": False, "reply": "", "holding_reply": "", "question": "?"})
    return ScriptedLLM(answer)
