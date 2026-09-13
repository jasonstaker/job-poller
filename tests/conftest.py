"""Offline HTTP doubles so handler tests are deterministic and never touch the network."""

from __future__ import annotations

import json
import pathlib

import pytest

import handlers

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}", response=self)


class FakeSession:
    """Maps an exact URL to a FakeResponse. Records every call for assertions."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, headers or {}))
        if url not in self.routes:
            return FakeResponse(404, {"error": "not found"})
        result = self.routes[url]
        return result() if callable(result) else result


@pytest.fixture
def ctx_factory():
    def make(routes):
        session = FakeSession(routes)
        ctx = handlers.FetchContext(session=session, jitter=False)
        return ctx, session

    return make


@pytest.fixture
def load_fixture():
    def load(name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load
