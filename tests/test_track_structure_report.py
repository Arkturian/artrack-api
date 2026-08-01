"""Regression tests for the track structure report endpoint (Issue #676)."""

import asyncio

import pytest
from fastapi import HTTPException

from artrack.routes import routes_routes


class _Query:
    def __init__(self, track):
        self._track = track

    def filter(self, *_args):
        return self

    def first(self):
        return self._track


class _Database:
    def __init__(self, track):
        self._track = track

    def query(self, *_args):
        return _Query(self._track)


def test_structure_report_uses_configured_internal_api(monkeypatch):
    captured = {}

    def fake_report(**kwargs):
        captured.update(kwargs)
        return "report"

    monkeypatch.setattr(routes_routes, "can_read_track", lambda *_args: True)
    monkeypatch.setattr(routes_routes, "generate_track_report", fake_report)
    monkeypatch.setattr(routes_routes.settings, "AI_BASE_URL", "http://127.0.0.1:8001/")
    monkeypatch.setattr(routes_routes.settings, "API_KEY", "configured-key")

    response = asyncio.run(
        routes_routes.get_track_structure_report(
            track_id=30,
            full=True,
            db=_Database(object()),
            current_user=object(),
        )
    )

    assert response.body == b"report"
    assert captured == {
        "track_id": 30,
        "show_descriptions": True,
        "api_key": "configured-key",
        "base_url": "http://127.0.0.1:8001",
    }


def test_structure_report_converts_generator_failure_to_bad_gateway(monkeypatch):
    monkeypatch.setattr(routes_routes, "can_read_track", lambda *_args: True)
    monkeypatch.setattr(
        routes_routes,
        "generate_track_report",
        lambda **_kwargs: (_ for _ in ()).throw(ConnectionError("upstream failed")),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            routes_routes.get_track_structure_report(
                track_id=30,
                db=_Database(object()),
                current_user=object(),
            )
        )

    assert raised.value.status_code == 502
    assert raised.value.detail == "Track structure report could not be generated."
