"""lev-local wiring with a fake engine in place of the 4B model."""

import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from tokentriage import Router, RouterConfig
from tokentriage.classifiers.base import LEV_QUESTIONS
from tokentriage.classifiers.lev_local import LevLocalClassifier
from tokentriage.features import extract


class FakeEngine:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.seen = []

    def system_one(self, state, questions):
        self.seen.append((state, questions))
        time.sleep(self.delay)
        return SimpleNamespace(
            answers={
                "tier": SimpleNamespace(probabilities={"simple": 0.05, "standard": 0.1, "complex": 0.85}),
                "needs_reasoning": SimpleNamespace(noul=0.9),
                "needs_code": SimpleNamespace(noul=0.7),
            }
        )


@pytest.fixture(autouse=True)
def reset_engine():
    yield
    LevLocalClassifier._engine = None
    LevLocalClassifier._load_error = None
    LevLocalClassifier._loader = None


def _decide(clf, text="Prove the scheduler is deadlock-free"):
    return Router(RouterConfig(), clf).decide("anthropic", "claude-sonnet-5", extract([HumanMessage(text)]))


def test_uses_loaded_engine():
    engine = FakeEngine()
    LevLocalClassifier._engine = engine
    d = _decide(LevLocalClassifier())
    assert d.model == "claude-opus-5-5" and d.signals.source == "lev-local"
    state, questions = engine.seen[0]
    assert "deadlock-free" in state and questions is LEV_QUESTIONS


def test_slow_engine_times_out_to_heuristic():
    LevLocalClassifier._engine = FakeEngine(delay=0.5)
    d = _decide(LevLocalClassifier(timeout_s=0.05))
    assert d.signals.source == "heuristic" and "timeout" in d.reason


def test_load_failure_falls_back():
    LevLocalClassifier._load_error = ImportError("no torch")
    d = _decide(LevLocalClassifier(), "What is 2+2?")
    assert d.signals.source == "heuristic" and d.model == "claude-haiku-4-5"
