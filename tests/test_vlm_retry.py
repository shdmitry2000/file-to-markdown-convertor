"""VLM page retries are the shared layer's (1.4.8.5): a rate limit waits it out, a
timeout retries briefly, a daily cap stops at once. The full matrix is in the root
suite (tests/shared/test_llm_vision_retry.py); this checks a page goes through it.

On the sandbox (2026-10-21) Vertex answered dbank's parallel page calls with 429
RESOURCE_EXHAUSTED; the converter's own retry (VLM_RATE_LIMIT_*) waited them out.
That retry is gone — one retry for every module now.
"""

from unittest.mock import patch

import pytest

from app.converters.vlm import VLMConverter
from shared.llm_factory.core import resilience
from shared.llm_factory.core.resilience import LLMDailyLimitError, resilient_sync


class _Err(Exception):
    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.status_code = status_code


class _Client:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    @resilient_sync
    def vision_sync(self, prompt, images):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(resilience.time, "sleep", waited.append)
    resilience.reset_limiter()
    return waited


def _page(client):
    with patch("app.converters.vlm_lane.client", return_value=client):
        return VLMConverter().transcribe_page("img")


def test_a_rate_limited_page_is_waited_out(sleeps):
    client = _Client(_Err("RESOURCE_EXHAUSTED", 429), "# page")
    assert _page(client) == "# page"
    assert client.calls == 2 and len(sleeps) == 1


def test_a_timed_out_page_is_retried_briefly(sleeps):
    client = _Client(_Err("gateway timeout", 504), "# page")
    assert _page(client) == "# page"
    assert sleeps == [1.0]


def test_a_daily_cap_stops_the_page_at_once(sleeps):
    client = _Client(_Err("Token limit exceeded: tokens per day", 429))
    with pytest.raises(LLMDailyLimitError, match="daily"):
        _page(client)
    assert client.calls == 1 and sleeps == []


def test_a_bad_request_fails_at_once(sleeps):
    client = _Client(_Err("bad request", 400))
    with pytest.raises(_Err):
        _page(client)
    assert client.calls == 1 and sleeps == []
