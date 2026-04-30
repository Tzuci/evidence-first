import uuid


def test_create_get_project(client):
    name = f"test-proj-{uuid.uuid4()}"
    r = client.post("/api/v1/projects", json={"name": name, "mode_default": "closed_corpus"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r2 = client.get(f"/api/v1/projects/{pid}")
    assert r2.status_code == 200
    assert r2.json()["name"] == name


def test_create_project_conflict(client):
    name = f"test-conflict-{uuid.uuid4()}"
    r1 = client.post("/api/v1/projects", json={"name": name})
    assert r1.status_code == 201
    r2 = client.post("/api/v1/projects", json={"name": name})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "RESOURCE_CONFLICT"


def test_list_projects_paginated(client):
    # Create three projects.
    base = f"test-list-{uuid.uuid4()}"
    for i in range(3):
        r = client.post("/api/v1/projects", json={"name": f"{base}-{i}"})
        assert r.status_code == 201

    r = client.get("/api/v1/projects?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert len(body["items"]) <= 2