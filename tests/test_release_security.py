"""Release checks for the evidence and browser authentication boundaries."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from aiqa.api import app
from aiqa.config import settings
from aiqa.access import authorize_artifact
from aiqa.storage import Record, session, uid
from aiqa.tools import dispatch
from aiqa.workspace import project_scope


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data", tmp_path)
    a, b = uid("PROJECT"), uid("PROJECT")
    own, other, inspection = uid("RUN"), uid("RUN"), uid("INS")
    with session() as db:
        for identity in [a, b]:
            db.add(Record(id=identity, kind="project", payload={"id": identity, "name": identity}))
    with project_scope(a), session() as db:
        db.add(Record(id=own, kind="run", status="passed", payload={"id": own, "status": "passed"}))
        db.add(Record(id=uid("TASK"), kind="task", payload={"inspections": [{"id": inspection}]}))
    with project_scope(b), session() as db:
        db.add(Record(id=other, kind="run", status="passed", payload={"id": other, "status": "passed"}))
    for relative, content in [(f"runs/{own}/nested/log.txt", "own evidence"),
                              (f"runs/{other}/secret.txt", "other project evidence"),
                              (f"inspections/{inspection}/page.xml", "<screen/>"),
                              ("private.txt", "private workspace data")]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    try:
        yield tmp_path, a, b, own, other, inspection
    finally:
        with session() as db:
            for row in db.scalars(select(Record).where(Record.project_id.in_([a, b]) | Record.id.in_([a, b]))):
                db.delete(row)


def test_normal_evidence_works_and_direct_other_project_is_denied(evidence):
    root, a, b, own, other, inspection = evidence
    client = TestClient(app, headers={"Authorization": "Bearer " + settings.access_token, "X-AIQA-Project": a})
    response = client.get(f"/api/artifacts/runs/{own}/nested/log.txt")
    assert response.status_code == 200 and response.text == "own evidence"
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get(f"/api/artifacts/runs/{other}/secret.txt").status_code == 404
    with project_scope(a):
        assert dispatch("artifacts_read", {"path": f"runs/{own}/nested/log.txt"})["content"] == "own evidence"
        assert authorize_artifact(f"inspections/{inspection}/page.xml") == root / f"inspections/{inspection}/page.xml"
    with project_scope(b), pytest.raises(ValueError):
        authorize_artifact(f"inspections/{inspection}/page.xml")


@pytest.mark.parametrize("tail", ["../OTHER/secret.txt", "../../private.txt", "nested/./log.txt", "nested//log.txt", r"nested\log.txt", "nested/log.txt\x00"])
def test_path_traversal_and_ambiguous_paths_are_rejected(evidence, tail):
    _, a, _, own, _, _ = evidence
    with project_scope(a), pytest.raises(ValueError):
        dispatch("artifacts_read", {"path": f"runs/{own}/{tail}"})


def test_encoded_http_traversal_does_not_cross_project_boundary(evidence):
    _, a, _, own, other, _ = evidence
    client = TestClient(app, headers={"Authorization": "Bearer " + settings.access_token, "X-AIQA-Project": a})
    response = client.get(f"/api/artifacts/runs/{own}/%2e%2e/{other}/secret.txt")
    assert response.status_code == 400
    assert "other project evidence" not in response.text


@pytest.mark.parametrize("private", [False, True])
def test_symlink_cannot_escape_the_authorized_run(evidence, private):
    root, a, _, own, other, _ = evidence
    link = root / f"runs/{own}/leak.txt"
    link.symlink_to(root / ("private.txt" if private else f"runs/{other}/secret.txt"))
    client = TestClient(app, headers={"Authorization": "Bearer " + settings.access_token, "X-AIQA-Project": a})
    assert client.get(f"/api/artifacts/runs/{own}/leak.txt").status_code == 400
    with project_scope(a), pytest.raises(ValueError):
        dispatch("artifacts_read", {"path": f"runs/{own}/leak.txt"})


@pytest.mark.parametrize("path,body", [
    ("/api/auth/login", {"username": "release_test", "password": "fake-test-password"}),
    ("/api/auth/setup", {"username": "release_test", "password": "fake-test-password"}),
    ("/api/session", {"token": "fake-test-token"}),
])
def test_cross_origin_public_authentication_is_rejected_before_credentials(path, body, monkeypatch):
    from aiqa import authentication
    def unexpected(*args):
        raise AssertionError("cross-origin authentication must not reach the account operation")
    monkeypatch.setattr(authentication, "login_account", unexpected)
    monkeypatch.setattr(authentication, "setup_account", unexpected)
    response = TestClient(app).post(path, json=body, headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_same_origin_browser_requests_and_originless_clients_still_work():
    client = TestClient(app, headers={"Authorization": "Bearer " + settings.access_token})
    assert client.post("/api/tools/cases_list", json={}, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/tools/cases_list", json={}).status_code == 200
    assert client.get("/api/auth/me").headers["cache-control"] == "private, no-store"
