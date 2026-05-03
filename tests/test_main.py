import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

import database
import zmq_receiver as zmq_mod


@pytest.fixture
def client():
    with (
        patch.object(database, "connect", new_callable=AsyncMock),
        patch.object(database, "disconnect", new_callable=AsyncMock),
        patch.object(zmq_mod.zmq_receiver, "start"),
        patch.object(zmq_mod.zmq_receiver, "stop"),
    ):
        from main import app
        with TestClient(app) as c:
            yield c


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_stream_live_returns_stub(client):
    resp = client.get("/stream/cam_01/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_stream_vod_returns_stub(client):
    resp = client.get("/stream/cam_01/vod")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_tracking_returns_stub(client):
    resp = client.get("/tracking/cam_01")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_alerts_returns_stub(client):
    resp = client.get("/alerts")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_settings_get_returns_stub(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_notes_get_returns_stub(client):
    resp = client.get("/notes")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}
