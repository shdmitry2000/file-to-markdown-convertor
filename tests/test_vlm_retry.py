"""VLM page retries: a rate limit waits it out, a timeout retries briefly, a bad request fails.

On the sandbox (2026-10-21) Vertex answered dbank's parallel page calls with 429
RESOURCE_EXHAUSTED. Every exception was retried as "timed out" after 1 s and 2 s and
then the whole conversion aborted, so a throttled minute failed whole documents and
the log never said why.
"""

import logging

import pytest

import app.converters.vlm as vlm
from app.converters.vlm import VLMConverter


class _Err(Exception):
    def __init__(self, msg: str, status_code: int | None = None, headers: dict | None = None):
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code
        if headers is not None:
            self.response = type("R", (), {"headers": headers})()


class APITimeoutError(Exception):
    """Named like openai's / litellm's timeout errors."""


def _converter(outcomes):
    """A converter whose page call returns or raises `outcomes` in order."""
    conv = object.__new__(VLMConverter)
    calls = iter(outcomes)

    def page(_img):
        out = next(calls)
        if isinstance(out, Exception):
            raise out
        return out

    conv._transcribe_page = page
    return conv


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(vlm.time, "sleep", waited.append)
    return waited


def test_rate_limit_waits_long_and_outlasts_the_short_budget(sleeps):
    conv = _converter([_Err("Too Many Requests", 429)] * 4 + ["page"])
    assert conv.transcribe_page("img") == "page"
    assert len(sleeps) == 4                                   # more than the 3 a timeout gets
    assert all(s >= vlm._RATE_LIMIT_BASE_DELAY_S * 0.5 for s in sleeps)
    assert max(sleeps) <= vlm._RATE_LIMIT_MAX_DELAY_S


def test_resource_exhausted_without_a_status_is_a_rate_limit(sleeps):
    conv = _converter([_Err('{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}'), "page"])
    assert conv.transcribe_page("img") == "page"
    assert sleeps[0] >= vlm._RATE_LIMIT_BASE_DELAY_S * 0.5


def test_retry_after_is_honoured(sleeps):
    conv = _converter([_Err("slow down", 429, headers={"retry-after": "42"}), "page"])
    assert conv.transcribe_page("img") == "page"
    assert sleeps == [42.0]


def test_rate_limit_gives_up_after_its_budget(sleeps):
    conv = _converter([_Err("Too Many Requests", 429)] * vlm._RATE_LIMIT_ATTEMPTS)
    with pytest.raises(_Err):
        conv.transcribe_page("img")
    assert len(sleeps) == vlm._RATE_LIMIT_ATTEMPTS - 1


def test_timeout_keeps_the_short_retry(sleeps):
    conv = _converter([APITimeoutError("read timed out")] * vlm._MAX_RETRY_ATTEMPTS)
    with pytest.raises(APITimeoutError):
        conv.transcribe_page("img")
    assert sleeps == [1.0, 2.0]


def test_bad_request_is_not_retried(sleeps):
    conv = _converter([_Err("invalid image", 400), "never"])
    with pytest.raises(_Err):
        conv.transcribe_page("img")
    assert sleeps == []


def test_the_log_names_the_real_error(sleeps, caplog):
    conv = _converter([_Err("Resource exhausted. Please try again later.", 429), "page"])
    with caplog.at_level(logging.WARNING, logger="app.converters.vlm"):
        conv.transcribe_page("img")
    assert "rate limited" in caplog.text and "Resource exhausted" in caplog.text
