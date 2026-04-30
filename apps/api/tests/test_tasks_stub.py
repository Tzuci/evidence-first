import uuid


def _create_project(client) -> str:
    r = client.post("/api/v1/projects", json={"name": f"task-proj-{uuid.uuid4()}"})
    assert r.status_code == 201
    return r.json()["id"]


def test_create_task_closed_corpus(client):
    pid = _create_project(client)
    r = client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "objective": "what is X?", "mode": "closed_corpus"},
    )
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["mode"] == "closed_corpus"
    assert task["status"] == "created"

    r2 = client.get(f"/api/v1/tasks/{task['id']}")
    assert r2.status_code == 200


def test_reject_non_closed_corpus(client):
    pid = _create_project(client)
    r = client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "objective": "no", "mode": "verified_web"},
    )
    # Pydantic rejects at the type level (Literal["closed_corpus"]) with VALIDATION_ERROR.
    assert r.status_code in (400, 422)
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_idempotency_key_returns_same_task(client):
    pid = _create_project(client)
    headers = {"Idempotency-Key": f"idem-{uuid.uuid4()}"}
    body = {"project_id": pid, "objective": "idem test", "mode": "closed_corpus"}
    r1 = client.post("/api/v1/tasks", json=body, headers=headers)
    r2 = client.post("/api/v1/tasks", json=body, headers=headers)
    assert r1.status_code == 201
    assert r2.status_code in (200, 201)
    assert r1.json()["id"] == r2.json()["id"]