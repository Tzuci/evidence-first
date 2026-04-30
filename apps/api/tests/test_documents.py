"""End-to-end tests for /documents.

Rerun-safety (8.2a-patch):
  - Fresh project per test invocation.
  - Dedup test uses unique payloads per test invocation; the assertion is on
    a single row in storage_blobs *for that hash*, not on a global count.
"""
from __future__ import annotations

import io
import uuid

import pytest
from sqlalchemy import text

from app.db import get_engine


def _new_project(client) -> str:
    r = client.post("/api/v1/projects", json={"name": f"docs-{uuid.uuid4()}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(client, pid: str, name: str, content: bytes, mime: str | None = None):
    files = {"file": (name, io.BytesIO(content), mime or "text/plain")}
    return client.post(f"/api/v1/projects/{pid}/documents", files=files)


def test_upload_txt_ok(client):
    pid = _new_project(client)
    payload = f"hello world {uuid.uuid4()}\n".encode("utf-8")
    r = _upload(client, pid, "hello.txt", payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["filename"] == "hello.txt"
    assert body["tier"] == "user_provided"
    assert body["size_bytes"] > 0


def test_upload_md_ok(client):
    pid = _new_project(client)
    payload = f"# Title\n\ncontent {uuid.uuid4()}\n".encode("utf-8")
    r = _upload(client, pid, "notes.md", payload, mime="text/markdown")
    assert r.status_code == 201
    body = r.json()
    assert body["filename"] == "notes.md"


def test_upload_invalid_extension_rejected(client):
    pid = _new_project(client)
    r = _upload(client, pid, "evil.exe", b"MZ", mime="application/octet-stream")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_upload_dedup_same_file_twice_creates_one_blob(client):
    pid = _new_project(client)
    payload = f"deterministic content for dedup test {uuid.uuid4()}\n".encode("utf-8")

    r1 = _upload(client, pid, "a.txt", payload)
    assert r1.status_code == 201
    r2 = _upload(client, pid, "b.txt", payload)
    assert r2.status_code == 201

    h1 = r1.json()["content_hash"]
    h2 = r2.json()["content_hash"]
    assert h1 == h2

    with get_engine().connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM storage_blobs WHERE content_hash = :h"),
            {"h": h1},
        ).scalar_one()
    assert int(n) == 1


def test_get_and_list_documents(client):
    pid = _new_project(client)
    payload = f"abc {uuid.uuid4()}".encode("utf-8")
    r = _upload(client, pid, "x.txt", payload)
    assert r.status_code == 201
    did = r.json()["id"]

    g = client.get(f"/api/v1/documents/{did}")
    assert g.status_code == 200
    assert g.json()["id"] == did

    l = client.get(f"/api/v1/projects/{pid}/documents")
    assert l.status_code == 200
    items = l.json()["items"]
    assert any(it["id"] == did for it in items)


def test_create_task_with_document_ids_attaches_and_audits(client):
    pid = _new_project(client)
    up = _upload(client, pid, "src.txt", f"answer {uuid.uuid4()}\n".encode("utf-8"))
    did = up.json()["id"]

    rt = client.post(
        "/api/v1/tasks",
        json={
            "project_id": pid,
            "objective": "summarize",
            "mode": "closed_corpus",
            "document_ids": [did],
        },
    )
    assert rt.status_code == 201, rt.text
    tid = rt.json()["id"]

    tdocs = client.get(f"/api/v1/tasks/{tid}/documents")
    assert tdocs.status_code == 200
    assert len(tdocs.json()["items"]) == 1
    assert tdocs.json()["items"][0]["document_id"] == did

    aud = client.get(f"/api/v1/tasks/{tid}/audit")
    assert aud.status_code == 200
    items = aud.json()["items"]
    types = [it["event_type"] for it in items[:2]]
    assert types == ["task.created", "task.docs_attached"]


def test_create_task_with_foreign_document_id_rejected(client):
    pid_a = _new_project(client)
    pid_b = _new_project(client)
    up = _upload(client, pid_a, "in_a.txt", f"foo {uuid.uuid4()}".encode("utf-8"))
    did = up.json()["id"]
    r = client.post(
        "/api/v1/tasks",
        json={
            "project_id": pid_b,
            "objective": "x",
            "mode": "closed_corpus",
            "document_ids": [did],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_task_with_unknown_document_id_returns_404(client):
    pid = _new_project(client)
    bogus = str(uuid.uuid4())
    r = client.post(
        "/api/v1/tasks",
        json={
            "project_id": pid,
            "objective": "x",
            "mode": "closed_corpus",
            "document_ids": [bogus],
        },
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert bogus in r.json()["error"]["details"]["missing"]


def test_create_task_with_duplicate_document_ids_rejected(client):
    pid = _new_project(client)
    up = _upload(client, pid, "dup.txt", f"dup {uuid.uuid4()}".encode("utf-8"))
    did = up.json()["id"]
    r = client.post(
        "/api/v1/tasks",
        json={
            "project_id": pid,
            "objective": "x",
            "mode": "closed_corpus",
            "document_ids": [did, did],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "duplicate" in r.json()["error"]["details"]


def test_create_task_with_multiple_valid_document_ids(client):
    pid = _new_project(client)
    up1 = _upload(client, pid, "a.txt", f"alpha {uuid.uuid4()}".encode("utf-8"))
    up2 = _upload(client, pid, "b.txt", f"beta {uuid.uuid4()}".encode("utf-8"))
    did1, did2 = up1.json()["id"], up2.json()["id"]
    r = client.post(
        "/api/v1/tasks",
        json={
            "project_id": pid,
            "objective": "multi",
            "mode": "closed_corpus",
            "document_ids": [did1, did2],
        },
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    tdocs = client.get(f"/api/v1/tasks/{tid}/documents")
    assert tdocs.status_code == 200
    items = tdocs.json()["items"]
    assert {it["document_id"] for it in items} == {did1, did2}
    # position is preserved.
    by_pos = sorted(items, key=lambda it: it["position"])
    assert by_pos[0]["document_id"] == did1
    assert by_pos[1]["document_id"] == did2