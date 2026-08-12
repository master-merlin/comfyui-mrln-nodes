"""The aiohttp adapter around the pure handlers.

register_routes() is the pack's only contact with `server`/`aiohttp`, and its
adapt() wrapper is where every request is size-capped, JSON-checked and turned
into the plain dict payload the handlers expect. That layer used to be waived
as 'straight-line, verified during UAT' — but it is also the only place where
a live request and a pytest call differ (GET values are strings in the browser
and rich types in tests), and the body cap is a real security boundary. These
tests drive the REAL adapter with a stubbed PromptServer and aiohttp's mocked
requests, over a probe route table so the assertions are about the adapter
rather than about any one handler.
"""

import asyncio
import json
import sys
import types

import pytest
import support  # noqa: F401

pytest.importorskip("aiohttp")

from aiohttp.test_utils import make_mocked_request

from mrln import promptapi
from mrln.promptapi import routes as routes_mod
from mrln.promptlib import Library

CAP = promptapi.MAX_BODY_BYTES


class _Body:
    """A request body stream. `chunk_size` mimics the real thing: read(n)
    hands back what is buffered NOW, never a promise of n bytes — the
    adapter has to loop to EOF rather than assume one read suffices."""

    def __init__(self, data, chunk_size=None):
        self._data = data
        self._chunk = chunk_size

    async def read(self, n=-1):
        take = min(n, self._chunk) if (self._chunk and n > 0) else n
        chunk, self._data = self._data[:take], self._data[take:]
        return chunk


def _fake_server(monkeypatch, registered):
    class _Routes:
        def __getattr__(self, method):
            def register(path):
                def decorate(endpoint):
                    registered[(method, path)] = endpoint
                    return endpoint

                return decorate

            return register

    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(routes=_Routes()))
    monkeypatch.setitem(sys.modules, "server", server)


def _register(monkeypatch, table):
    monkeypatch.setattr(routes_mod, "ROUTES", table)
    registered = {}
    _fake_server(monkeypatch, registered)
    assert promptapi.register_routes() is True
    return registered


@pytest.fixture()
def probe(tmp_path, monkeypatch):
    """register_routes() over a two-entry probe table -> (endpoints, log)."""
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))
    seen = []

    def handler(lib, payload):
        seen.append(payload)
        return 200, {"ok": True, "payload": payload}

    registered = _register(
        monkeypatch,
        (
            ("get", "/mrln/test/probe", handler, False),
            ("post", "/mrln/test/probe", handler, True),
        ),
    )
    return registered, seen


def drive(endpoint, request):
    response = asyncio.run(endpoint(request))
    return response.status, json.loads(response.body)


def post(raw, *, headers=None, chunk_size=None):
    return make_mocked_request(
        "POST", "/mrln/test/probe", headers=headers, payload=_Body(raw, chunk_size)
    )


# -- registration -------------------------------------------------------------


def test_every_route_in_the_table_is_actually_attached(tmp_path, monkeypatch):
    """The soft-fail-outside-ComfyUI path is covered elsewhere; this is the
    success path — the real table reaches the server, method and all."""
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))
    registered = {}
    _fake_server(monkeypatch, registered)
    assert promptapi.register_routes() is True
    assert set(registered) == {(m, p) for m, p, _h, _b in promptapi.ROUTES}


# -- body cap -----------------------------------------------------------------


def test_content_length_over_the_cap_is_refused_before_a_byte_is_read(probe):
    endpoints, seen = probe
    request = post(b"", headers={"Content-Length": str(CAP + 1)})

    async def must_not_read(n=-1):
        raise AssertionError("body was read despite an oversized Content-Length")

    request.content.read = must_not_read
    status, body = drive(endpoints[("post", "/mrln/test/probe")], request)
    assert status == 413 and "too large" in body["error"] and body["remediation"]
    assert seen == []


def test_a_chunked_body_over_the_cap_is_still_refused(probe):
    """Content-Length is a hint, not a promise: a chunked upload carries
    none. The cap that actually holds is on the bytes read."""
    endpoints, seen = probe
    oversized = b'{"pad": "' + b"x" * (CAP + 64) + b'"}'
    status, body = drive(endpoints[("post", "/mrln/test/probe")], post(oversized))
    assert status == 413 and "too large" in body["error"]
    assert seen == []


def test_a_body_at_the_cap_is_accepted(probe):
    """Off-by-one guard on the other side: exactly CAP bytes must pass."""
    endpoints, seen = probe
    pad = "x" * (CAP - len(b'{"pad": ""}'))
    raw = json.dumps({"pad": pad}).encode()
    assert len(raw) == CAP
    status, _body = drive(endpoints[("post", "/mrln/test/probe")], post(raw))
    assert status == 200 and seen[0]["pad"] == pad


def test_a_body_split_across_reads_is_reassembled(probe):
    """read(n) returns what is buffered now; a 7-byte drip feed proves the
    adapter loops to EOF instead of trusting the first read."""
    endpoints, seen = probe
    payload = {"slug": "vehicle/car/color/paint", "data": {"items": [1, 2, 3]}}
    raw = json.dumps(payload).encode()
    status, _body = drive(endpoints[("post", "/mrln/test/probe")], post(raw, chunk_size=7))
    assert status == 200 and seen == [payload]


# -- JSON shape ---------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"{{{", b"", b"not json at all", b'{"unclosed": '])
def test_an_unparsable_body_is_a_400_not_a_500(probe, raw):
    endpoints, seen = probe
    status, body = drive(endpoints[("post", "/mrln/test/probe")], post(raw))
    assert status == 400 and "not valid JSON" in body["error"]
    assert seen == []


@pytest.mark.parametrize("raw", [b"[1, 2]", b'"a string"', b"42", b"null"])
def test_a_non_object_body_is_a_400(probe, raw):
    """Handlers call payload.get(...) — a JSON array or scalar has to be
    refused at the door instead of exploding inside the handler."""
    endpoints, seen = probe
    status, body = drive(endpoints[("post", "/mrln/test/probe")], post(raw))
    assert status == 400 and "JSON object" in body["error"]
    assert seen == []


def test_post_passes_the_parsed_object_through_untouched(probe):
    endpoints, seen = probe
    payload = {"air": "urn:air:sdxl:lora:civitai:1@2", "start": True, "n": 3}
    request = post(json.dumps(payload).encode())
    status, body = drive(endpoints[("post", "/mrln/test/probe")], request)
    assert status == 200 and body["ok"] is True
    # POST KEEPS 'start' — it is the deliberate write channel
    assert seen == [payload]


# -- GET query ----------------------------------------------------------------


def test_get_query_reaches_the_handler_as_plain_strings(probe):
    """Every value a GET handler sees in production is a string, while pytest
    hands them ints and dicts. Pinning the real shape stops a handler from
    growing an `is True` / int comparison that only passes under pytest."""
    endpoints, seen = probe
    request = make_mocked_request("GET", "/mrln/test/probe?slug=color&n=3&flag=true")
    status, body = drive(endpoints[("get", "/mrln/test/probe")], request)
    assert status == 200
    assert seen == [{"slug": "color", "n": "3", "flag": "true"}]
    assert all(isinstance(value, str) for value in body["payload"].values())


def test_get_can_never_carry_the_start_flag(probe):
    """'start' is the one state-changing flag (LoRA download, model pull).
    GET values are strings, so `payload.get('start') is True` is already
    False — but the adapter strips it outright, so a bare cross-site GET
    (no CORS preflight) cannot even reach the branch."""
    endpoints, seen = probe
    request = make_mocked_request("GET", "/mrln/test/probe?air=urn:air:x&start=true")
    status, _body = drive(endpoints[("get", "/mrln/test/probe")], request)
    assert status == 200 and seen == [{"air": "urn:air:x"}]


def test_a_get_without_a_query_is_an_empty_payload(probe):
    endpoints, seen = probe
    status, _body = drive(
        endpoints[("get", "/mrln/test/probe")], make_mocked_request("GET", "/mrln/test/probe")
    )
    assert status == 200 and seen == [{}]


def test_a_get_body_is_never_read(probe):
    """GET routes are declared reads_body=False: even a body-bearing GET
    takes the query path, so no read cap is bypassed."""
    endpoints, seen = probe
    request = make_mocked_request("GET", "/mrln/test/probe?a=1", payload=_Body(b'{"a": 2}'))
    status, _body = drive(endpoints[("get", "/mrln/test/probe")], request)
    assert status == 200 and seen == [{"a": "1"}]


# -- handler answers ----------------------------------------------------------


def test_the_handlers_status_and_body_survive_the_adapter(tmp_path, monkeypatch):
    """The adapter must not rewrite what a handler decided: a 404 stays a 404
    carrying the handler's own remediation text."""
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))

    def refusing(lib, payload):
        return 404, {"error": "nope", "remediation": "try another"}

    registered = _register(monkeypatch, (("get", "/mrln/test/refuse", refusing, False),))
    status, body = drive(
        registered[("get", "/mrln/test/refuse")], make_mocked_request("GET", "/mrln/test/refuse")
    )
    assert status == 404 and body == {"error": "nope", "remediation": "try another"}


def test_a_handler_receives_a_real_library(tmp_path, monkeypatch):
    """The adapter opens the Library itself — handlers never do."""
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))
    got = []

    def inspecting(lib, payload):
        got.append(lib)
        return 200, {"slugs": len(lib.section_slugs())}

    registered = _register(monkeypatch, (("get", "/mrln/test/lib", inspecting, False),))
    status, body = drive(
        registered[("get", "/mrln/test/lib")], make_mocked_request("GET", "/mrln/test/lib")
    )
    assert status == 200 and body["slugs"] > 0  # the shipped factory content
    assert isinstance(got[0], Library)
