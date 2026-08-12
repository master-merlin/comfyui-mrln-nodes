"""The API-key invariant: a stored key is NEVER echoed by any API.

The LoRA download-status poll route is UNAUTHENTICATED and hands back
`detail` verbatim, so two things have to hold at once: the key must not be
in the download URL in the first place (it rides an `Authorization: Bearer`
header), and every client-visible string built in `promptapi/lora.py` must
be scrubbed on the way out — belt AND braces, because the presigned-CDN
fallback can still put a `token=` in a URL. All offline: urllib is canned.
"""

import hashlib
import json
import sys
import types
import urllib.error
import urllib.parse

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_roots

from mrln import promptapi
from mrln.promptapi import lora as lora_mod
from mrln.promptlib import Library

AIR = "urn:air:sdxl:lora:civitai:777@888"
# stored in settings.json and reached through the handlers
KEY = "civitai-key-77f3a9d2e1b4c6a8"
# handed straight to the worker/fetch layer, never through settings — so an
# assertion about it proves the scrubbing argument path, not the registry
DL_KEY = "civitai-dl-key-4b19e7c30af5"

BLOB = b"MRLN fake safetensors payload " * 32
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()


@pytest.fixture()
def lib(tmp_path):
    library = Library(*build_roots(tmp_path))
    library.user_root.mkdir(parents=True, exist_ok=True)
    (library.user_root / "settings.json").write_text(
        json.dumps({"civitai_api_key": KEY}), encoding="utf-8"
    )
    return library


# -- the scrubber itself ------------------------------------------------------


def test_scrub_redacts_token_query_values():
    scrub = lora_mod._scrub_secrets
    assert (
        scrub("https://civitai.com/api/download/models/888?token=abcdef123456")
        == "https://civitai.com/api/download/models/888?token=***"
    )
    # & ends the value: the rest of the query survives, readable for debugging
    assert (
        scrub("?type=Model&token=abcdef123456&format=SafeTensor")
        == "?type=Model&token=***&format=SafeTensor"
    )
    # quotes and angle brackets terminate it too (urllib wraps urls in both)
    assert (
        scrub('<urlopen error for "https://civitai.com/x?token=abcdef123456">')
        == '<urlopen error for "https://civitai.com/x?token=***">'
    )
    # case-insensitive, and the key's own casing survives the rewrite
    assert scrub("Token=ABCDEF123456 then token=abcdef123456") == "Token=*** then token=***"
    assert scrub("Civitai unreachable: timed out") == "Civitai unreachable: timed out"
    assert scrub(None) == "None"  # never raises, whatever it is handed


def test_scrub_redacts_a_key_literal_and_its_encoded_form():
    key = "sk/live+abc=12345678"
    encoded = urllib.parse.quote(key, safe="")
    out = lora_mod._scrub_secrets(f"401 for ?token={encoded} (sent {key})", key)
    assert key not in out and encoded not in out
    assert out.count("***") == 2


def test_scrub_leaves_short_words_alone():
    """The literal pass has a length floor — otherwise a 3-character 'key'
    would shred ordinary error text."""
    assert lora_mod._scrub_secrets("the red car stalled", "red") == "the red car stalled"


# -- canned urllib ------------------------------------------------------------


class _Canned:
    """Minimal stand-in for a urllib response: read()/read(n) + headers."""

    def __init__(self, body, headers=None):
        self._body = body
        self.headers = dict(headers or {})

    def read(self, size=None):
        if size is None:
            body, self._body = self._body, b""
            return body
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _meta(sha=BLOB_SHA):
    return {
        "files": [
            {
                "name": "bmw_m4_cs.safetensors",
                "primary": True,
                "hashes": {"SHA256": sha},
                "downloadUrl": "https://civitai.com/api/download/models/888",
            }
        ]
    }


def _can_urllib(monkeypatch, *, meta=None, download=BLOB):
    """Cans urllib.request.urlopen for the whole fetch and returns the request
    log. `download` may be a list: one entry consumed per download attempt
    (bytes = body, Exception = raised), the last one repeating."""
    responses = list(download) if isinstance(download, list) else [download]
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        if "/api/v1/model-versions/" in request.full_url:
            return _Canned(json.dumps(meta or _meta()).encode("utf-8"))
        item = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(item, Exception):
            raise item
        return _Canned(item, {"Content-Length": str(len(item))})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return calls


def _run_worker(tmp_path, monkeypatch, *, meta=None, download=BLOB, token=DL_KEY):
    """Drive the background worker synchronously; returns (status, calls, dest)."""
    calls = _can_urllib(monkeypatch, meta=meta, download=download)
    meta_headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if token:
        meta_headers["Authorization"] = f"Bearer {token}"
    dest = tmp_path / "loras"
    promptapi._LORA_DL_STATUS[AIR] = {
        "status": "downloading",
        "detail": "",
        "loaded": 0,
        "total": 0,
    }
    promptapi._lora_download_worker(AIR, meta_headers, token, 888, str(dest), "", None)
    return promptapi._LORA_DL_STATUS[AIR], calls, dest


def _forbidden(code=403):
    """What a presigned CDN answers when a forwarded Authorization header
    collides with the signature already in its query string."""
    return urllib.error.HTTPError(
        "https://cdn.example/presigned?X-Amz-Signature=deadbeef", code, "Forbidden", {}, None
    )


# -- the key stays out of the URL --------------------------------------------


def test_download_carries_the_key_in_a_header_not_the_url(tmp_path, monkeypatch):
    status, calls, dest = _run_worker(tmp_path, monkeypatch)
    assert status["status"] == "done", status
    assert (dest / "bmw_m4_cs.safetensors").read_bytes() == BLOB
    meta_req, dl_req = calls
    assert meta_req.get_header("Authorization") == f"Bearer {DL_KEY}"
    assert dl_req.get_header("Authorization") == f"Bearer {DL_KEY}"
    assert "token=" not in dl_req.full_url  # the secret never rides the query
    assert DL_KEY not in dl_req.full_url
    assert DL_KEY not in json.dumps(status)


def test_a_presigned_403_falls_back_to_the_query_token(tmp_path, monkeypatch):
    """Header auth is primary, but urllib forwards headers across the redirect
    to presigned storage, which rejects the extra Authorization. The retry
    keeps real downloads working — and stays scrubbed."""
    status, calls, dest = _run_worker(tmp_path, monkeypatch, download=[_forbidden(), BLOB])
    assert status["status"] == "done", status
    assert (dest / "bmw_m4_cs.safetensors").read_bytes() == BLOB
    _meta_req, first, retry = calls
    assert first.has_header("Authorization") and "token=" not in first.full_url
    assert not retry.has_header("Authorization")  # one auth mechanism at a time
    assert f"token={urllib.parse.quote(DL_KEY)}" in retry.full_url
    assert DL_KEY not in json.dumps(status)  # still never echoed


def test_a_non_auth_error_is_not_retried(tmp_path, monkeypatch):
    """Only 401/403 mean 'the header was the problem'; a 500 must not double
    the traffic on a multi-GB download."""
    status, calls, _dest = _run_worker(tmp_path, monkeypatch, download=[_forbidden(500)])
    assert status["status"] == "error"
    assert len(calls) == 2  # metadata + one download attempt, no retry


def test_no_token_no_fallback(tmp_path, monkeypatch):
    """Keyless downloads (public models) build no Authorization header at
    all, so a 403 is final."""
    status, calls, _dest = _run_worker(tmp_path, monkeypatch, download=[_forbidden()], token="")
    assert status["status"] == "error"
    assert len(calls) == 2
    assert not calls[1].has_header("Authorization")


# -- and every echoed string is scrubbed -------------------------------------


def test_worker_scrubs_a_token_bearing_url_out_of_the_polled_detail(tmp_path, monkeypatch, lib):
    boom = RuntimeError(
        f"502 from https://civitai.com/api/download/models/888?token={DL_KEY}&type=Model"
    )
    status, _calls, _dest = _run_worker(tmp_path, monkeypatch, download=[boom])
    assert status["status"] == "error"
    assert "token=***" in status["detail"]
    assert "&type=Model" in status["detail"]  # scrubbed, not truncated
    assert DL_KEY not in status["detail"]
    # …and that is exactly what the UNAUTHENTICATED poll route hands out
    code, body = promptapi.handle_lora_download(lib, {"air": AIR})
    assert code == 200 and body["status"] == "error"
    assert body["detail"] == status["detail"]
    assert DL_KEY not in json.dumps(body)


def test_worker_scrubs_the_key_quoted_into_a_url(tmp_path, monkeypatch):
    """The fallback percent-encodes the token, so the encoded form has to be
    redacted too — the regex catches this one, the literal pass backs it up."""
    quoted = urllib.parse.quote(DL_KEY)
    status, _calls, _dest = _run_worker(
        tmp_path, monkeypatch, download=[OSError(f"reset while GET /x?token={quoted}")]
    )
    assert status["detail"].endswith("token=***")
    assert DL_KEY not in status["detail"] and quoted not in status["detail"]


def test_worker_scrubs_a_verbatim_key_with_no_token_prefix(tmp_path, monkeypatch):
    """Not every leak looks like a query string: a backend that quotes the
    rejected credential back at us must not reach the client either."""
    status, _calls, _dest = _run_worker(
        tmp_path, monkeypatch, download=[RuntimeError(f"key '{DL_KEY}' is not valid")]
    )
    assert status["status"] == "error"
    assert DL_KEY not in status["detail"]
    assert status["detail"] == "key '***' is not valid"


def test_a_successful_detail_is_scrubbed_too(tmp_path, monkeypatch):
    status, _calls, _dest = _run_worker(tmp_path, monkeypatch)
    assert status["detail"] == "saved as bmw_m4_cs.safetensors"  # nothing to redact
    assert DL_KEY not in json.dumps(status)


# -- the stored key, through the real handlers -------------------------------


def _fake_folder_paths(monkeypatch, root, installed=("kit.safetensors",)):
    module = types.SimpleNamespace(
        get_filename_list=lambda kind: list(installed),
        get_folder_paths=lambda kind: [str(root)],
        get_full_path=lambda kind, name: str(root / name),
    )
    monkeypatch.setitem(sys.modules, "folder_paths", module)
    return module


def test_civitai_lookup_error_never_echoes_the_stored_key(lib, tmp_path, monkeypatch):
    root = tmp_path / "loras"
    root.mkdir(parents=True, exist_ok=True)
    (root / "kit.safetensors").write_bytes(b"not really a lora")
    _fake_folder_paths(monkeypatch, root)

    def urlopen(request, timeout=None):
        raise OSError(f"tunnel closed for {request.full_url}?token={KEY} (sent key {KEY})")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    code, body = promptapi.handle_lora_civitai(lib, {"name": "kit.safetensors"})
    assert code == 502
    assert "token=***" in body["error"]
    assert KEY not in json.dumps(body)
    # reading the key registered it, so a message assembled anywhere later —
    # another module, a worker thread — is scrubbed by the 1-argument contract
    assert KEY not in lora_mod._scrub_secrets(f"stale reference to {KEY}")


def test_node_path_failure_resolves_the_status_and_scrubs(lib, tmp_path, monkeypatch):
    """download_lora_by_air is the synchronous node path, but it publishes
    into the same polled dict — a failure must leave it resolved (never stuck
    on 'downloading') and carry no key, in the status or the raised message."""
    root = tmp_path / "loras"
    _fake_folder_paths(monkeypatch, root, installed=())
    _can_urllib(monkeypatch, download=[RuntimeError(f"gateway said no: https://cdn/x?token={KEY}")])
    with pytest.raises(RuntimeError) as excinfo:
        promptapi.download_lora_by_air(lib, AIR)
    message = str(excinfo.value)
    assert "token=***" in message and KEY not in message
    assert message.startswith("RuntimeError: ")  # class kept, message scrubbed
    assert excinfo.value.__cause__ is None  # no unscrubbed cause rides the log
    status = promptapi._LORA_DL_STATUS[AIR]
    assert status["status"] == "error" and KEY not in json.dumps(status)
