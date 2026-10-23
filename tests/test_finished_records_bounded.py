"""The API's in-memory conversion and chunk records are bounded.

conversion_status_db / conversion_details_db got one entry per conversion and
chunk_results_db the full chunk list per chunk task, and none was ever removed — a
pod's memory grew with every file it had converted.
"""

import pytest


@pytest.fixture
def main(monkeypatch):
    """`app.api.main` binds its ZeroMQ sockets at import, so fake them (as the other
    API tests do), and start each test from empty maps."""
    from tests.zmq_fakes import FakeContext

    monkeypatch.setattr("zmq.Context", FakeContext)
    from app.api import main as m

    saved = {name: dict(getattr(m, name)) for name in (
        "conversion_status_db", "conversion_details_db", "latest_conversion_by_name",
        "chunk_status_db", "chunk_results_db")}
    for name in saved:
        getattr(m, name).clear()
    yield m
    for name, content in saved.items():
        getattr(m, name).clear()
        getattr(m, name).update(content)


def test_oldest_finished_conversions_go_first(main, monkeypatch):
    monkeypatch.setattr(main, "_MAX_FINISHED_CONVERSIONS", 2)
    for i, status in enumerate(["completed", "processing", "failed", "completed", "pending"]):
        main.conversion_status_db[f"c{i}"] = status
        main.conversion_details_db[f"c{i}"] = {"n": i}
        main.latest_conversion_by_name[f"f{i}.pdf"] = f"c{i}"

    main._prune_finished_records()

    # c0 was the oldest finished; c1 / c4 are still running and are never dropped.
    assert list(main.conversion_status_db) == ["c1", "c2", "c3", "c4"]
    assert "c0" not in main.conversion_details_db
    assert "f0.pdf" not in main.latest_conversion_by_name
    assert main.latest_conversion_by_name["f2.pdf"] == "c2"


def test_a_status_update_does_not_make_a_conversion_young_again(main, monkeypatch):
    monkeypatch.setattr(main, "_MAX_FINISHED_CONVERSIONS", 1)
    main.conversion_status_db["old"] = "pending"
    main.conversion_status_db["new"] = "completed"
    main.conversion_status_db["old"] = "completed"   # finishes later, still created first

    main._prune_finished_records()

    assert list(main.conversion_status_db) == ["new"]


def test_chunk_results_are_bounded(main, monkeypatch):
    monkeypatch.setattr(main, "_MAX_FINISHED_CHUNKS", 1)
    for i in range(3):
        main.chunk_status_db[f"k{i}"] = "completed"
        main.chunk_results_db[f"k{i}"] = {"chunks": ["x" * 100]}

    main._prune_finished_records()

    assert list(main.chunk_results_db) == ["k2"]
