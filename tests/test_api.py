from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(env):
    app = create_app(env)
    with TestClient(app) as c:
        c.app_state = app.state
        yield c


def test_upload_text_creates_syllabus_and_job(client):
    r = client.post("/syllabi", data={"text": (FIX / "sample_syllabus.txt").read_text()})
    assert r.status_code == 201
    sid = r.json()["id"]
    jobs = client.app_state.db.list_jobs()
    assert [(j["kind"], j["ref_id"], j["status"]) for j in jobs] == [("extract_topics", sid, "queued")]
    body = client.get(f"/syllabi/{sid}").json()
    assert body["status"] == "queued" and body["topics"] == []


def test_upload_pdf(client):
    with open(FIX / "sample_syllabus.pdf", "rb") as fh:
        r = client.post("/syllabi", files={"file": ("syllabus.pdf", fh, "application/pdf")})
    assert r.status_code == 201
    syl = client.app_state.db.get_syllabus(r.json()["id"])
    assert syl["filename"] == "syllabus.pdf" and "Eigenvalues" in syl["raw_text"]


def test_upload_requires_input(client):
    assert client.post("/syllabi", data={}).status_code == 400


def test_upload_rejects_unreadable_pdf(client):
    r = client.post("/syllabi", files={"file": ("x.pdf", b"%PDF-1.4 nonsense", "application/pdf")})
    assert r.status_code == 422


def test_get_unknown_syllabus_404(client):
    assert client.get("/syllabi/999").status_code == 404


def test_get_syllabus_lists_topics(client):
    db = client.app_state.db
    sid = db.create_syllabus("text " * 20)
    db.insert_topics(sid, [{"title": "Vectors", "slug": "vectors", "objectives": ["a", "b", "c"]}])
    body = client.get(f"/syllabi/{sid}").json()
    assert body["topics"][0]["title"] == "Vectors"
    assert body["topics"][0]["status"] == "queued"
