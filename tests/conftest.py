"""Offline HTTP doubles so handler tests are deterministic and never touch the network."""

from __future__ import annotations

import json
import pathlib
from typing import NamedTuple

import pytest

import handlers

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class Call(NamedTuple):
    """One recorded request.

    `body` matters because Workday sends the SAME url for every page and carries `offset`,
    `limit` and `searchText` in the POST body -- without recording it, none of the
    pagination facts can be asserted at all.
    """

    method: str
    url: str
    headers: dict
    body: dict | None


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
    """Maps an exact URL to a FakeResponse, or to a callable that builds one.

    A route may be a FakeResponse (same answer every time) or a callable
    ``(method, url, body, headers) -> FakeResponse``. The callable form is what lets a
    fake serve different pages for the same url based on the `offset` in the request body,
    which is the only way to test Workday pagination.
    """

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[Call] = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append(Call(method, url, headers or {}, json))
        if url not in self.routes:
            return FakeResponse(404, {"error": "not found"})
        result = self.routes[url]
        return result(method, url, json, headers) if callable(result) else result


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
