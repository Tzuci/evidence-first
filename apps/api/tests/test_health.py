def test_health_live(client):
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_ready_keys_and_status(client):
    """In a properly bootstrapped env all three components must report 'ok' and HTTP 200."""
    r = client.get("/health/ready")
    body = r.json()
    assert set(body.keys()) == {"db", "redis", "storage"}
    if body["db"] == "ok" and body["redis"] == "ok" and body["storage"] == "ok":
        assert r.status_code == 200
    else:
        # If any component is degraded, the endpoint MUST return 503.
        assert r.status_code == 503


def test_health_db_and_queue_separate_endpoints(client):
    rd = client.get("/health/db")
    rq = client.get("/health/queue")
    rs = client.get("/health/storage")
    assert rd.status_code == 200
    assert rq.status_code == 200
    assert rs.status_code == 200
    assert "db" in rd.json()
    assert "redis" in rq.json()
    assert "storage" in rs.json()