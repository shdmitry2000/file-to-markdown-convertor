"""Converted markdown is stored under an internal md5 name and served by job.

The converted directory is flat and shared by every space and system. Written as
`<stem>.md`, report.pdf / report.docx — or one name in two spaces — overwrote each
other, and a caller fetching by name could receive the other job's markdown. The
by-name route also mapped only .pdf/.docx/.doc/.xls* back to `<stem>.md`, so
.pptx, .csv and images always came back 404. The internal name is never part of
any answer: callers address a job, and the original name stays in the frontmatter.
"""

import hashlib
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def mock_zmq(monkeypatch):
    from tests.zmq_fakes import FakeContext

    monkeypatch.setattr("zmq.Context", FakeContext)


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.api import main

    monkeypatch.setattr(main.settings, "CONVERTED_FILES_DIR", str(tmp_path))
    monkeypatch.setattr(main, "latest_conversion_by_name", {})
    monkeypatch.setattr(main, "conversion_details_db", {})
    monkeypatch.setattr(main, "conversion_status_db", {})
    monkeypatch.setattr(main, "conversion_waiters", {})
    monkeypatch.setattr(main, "_output_readers", {})
    with TestClient(main.app) as c:
        yield c, main, tmp_path


def _finished(main, out, content):
    """A completed job whose worker reported an md5-named output."""
    cid, name = str(uuid.uuid4()), hashlib.md5(content.encode()).hexdigest() + ".md"
    (out / name).write_text(content, encoding="utf-8")
    main.conversion_details_db[cid] = {"status": "completed", "output_file": name}
    return cid


def test_content_is_served_by_job(api):
    client, main, out = api
    a = _finished(main, out, "# from report.pdf")
    b = _finished(main, out, "# from report.docx")

    assert client.get(f"/convert/{a}/content").json()["content"] == "# from report.pdf"
    assert client.get(f"/convert/{b}/content").json()["content"] == "# from report.docx"
    assert client.get(f"/convert/{uuid.uuid4()}/content").status_code == 404


def test_a_reported_name_that_is_not_internal_is_refused(api):
    client, main, _out = api
    cid = str(uuid.uuid4())
    main.conversion_details_db[cid] = {"output_file": "../../etc/passwd"}
    assert client.get(f"/convert/{cid}/content").status_code == 404


def test_the_legacy_name_route_maps_to_the_latest_job(api):
    client, main, out = api
    main.latest_conversion_by_name["דוח, 2024 #2.pptx"] = _finished(main, out, "# slides")

    r = client.get("/converted/" + "%D7%93%D7%95%D7%97%2C%202024%20%232.pptx")
    assert r.status_code == 200 and r.json()["content"] == "# slides"


@pytest.mark.parametrize("name", ["deck.pptx", "table.csv", "scan.png", "report.pdf"])
def test_the_legacy_stem_fallback_covers_every_source_type(api, name):
    client, _main, out = api
    (out / f"{os.path.splitext(name)[0]}.md").write_text("# old output", encoding="utf-8")
    assert client.get(f"/converted/{name}").json()["content"] == "# old output"


def _aged(path, days):
    past = time.time() - days * 24 * 3600
    os.utime(path, (past, past))


def test_by_default_an_unfetched_output_goes_after_a_day(api):
    _client, main, out = api
    old, fresh = out / f"{'a' * 32}.md", out / f"{'b' * 32}.md"
    for p in (old, fresh):
        p.write_text("x", encoding="utf-8")
    _aged(old, 2)
    main._prune_converted_outputs()
    assert not old.exists() and fresh.exists()


def test_retention_zero_keeps_everything(api, monkeypatch):
    _client, main, out = api
    monkeypatch.setattr(main, "_CONVERTED_RETENTION_SECONDS", 0)
    old = out / f"{'a' * 32}.md"
    old.write_text("x", encoding="utf-8")
    _aged(old, 365)
    main._prune_converted_outputs()
    assert old.exists()


def test_with_a_retention_only_old_internal_outputs_go(api, monkeypatch):
    _client, main, out = api
    monkeypatch.setattr(main, "_CONVERTED_RETENTION_SECONDS", 24 * 3600)
    old, fresh, legacy = out / f"{'a' * 32}.md", out / f"{'b' * 32}.md", out / "report.md"
    debug_upload = out / "debug_דוח.pdf"
    for p in (old, fresh, legacy, debug_upload):
        p.write_text("x", encoding="utf-8")
    _aged(old, 3)
    _aged(legacy, 3)
    _aged(debug_upload, 3)

    main._prune_converted_outputs()

    assert not old.exists() and not debug_upload.exists()
    assert fresh.exists() and legacy.exists()


# --- the output is a hand-off: deleted once every waiting caller has fetched it ---

def _complete(main, out, content, waiters=1):
    """A job that finished through the result path, with `waiters` callers on it."""
    cid = str(uuid.uuid4())
    name = hashlib.md5(content.encode()).hexdigest() + ".md"
    (out / name).write_text(content, encoding="utf-8")
    main.conversion_status_db[cid] = "processing"
    main.conversion_waiters[cid] = waiters
    main._apply_result({"conversion_id": cid, "status": "completed", "output_file": name})
    return cid, out / name


def test_one_caller_reads_and_the_output_is_gone(api):
    client, main, out = api
    cid, path = _complete(main, out, "# one")
    assert client.get(f"/convert/{cid}/content").json()["content"] == "# one"
    assert not path.exists()
    assert client.get(f"/convert/{cid}/content").status_code == 404   # the client re-submits


def test_a_shared_conversion_stays_until_the_last_caller_reads(api):
    client, main, out = api
    cid, path = _complete(main, out, "# shared", waiters=2)
    assert client.get(f"/convert/{cid}/content").status_code == 200
    assert path.exists()
    assert client.get(f"/convert/{cid}/content").status_code == 200
    assert not path.exists()


def test_identical_bytes_from_two_jobs_share_one_file(api):
    client, main, out = api
    a, path = _complete(main, out, "# same")
    b, _ = _complete(main, out, "# same")
    client.get(f"/convert/{a}/content")
    assert path.exists()                       # job b has not fetched it yet
    assert client.get(f"/convert/{b}/content").json()["content"] == "# same"
    assert not path.exists()


def test_a_caller_cancelling_a_finished_job_releases_its_read(api):
    client, main, out = api
    cid, path = _complete(main, out, "# left", waiters=2)
    client.delete(f"/convert/{cid}")
    assert path.exists()
    client.get(f"/convert/{cid}/content")
    assert not path.exists()


def test_the_legacy_name_route_also_releases(api):
    client, main, out = api
    cid, path = _complete(main, out, "# legacy")
    main.latest_conversion_by_name["deck.pptx"] = cid
    assert client.get("/converted/deck.pptx").json()["content"] == "# legacy"
    assert not path.exists()


def test_a_debug_upload_is_removed_when_its_job_ends(api):
    _client, main, out = api
    upload = out / "debug_report.pdf"
    upload.write_bytes(b"%PDF")
    cid = str(uuid.uuid4())
    main.conversion_details_db[cid] = {"debug_input": str(upload)}
    main._apply_result({"conversion_id": cid, "status": "completed", "output_file": "a" * 32 + ".md"})
    assert not upload.exists()
