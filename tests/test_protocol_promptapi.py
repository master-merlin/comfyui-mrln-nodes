"""Pre-ship protocol burn-down for `mrln/promptapi/`.

One test per verified defect, each written to fail against the code as it
stood before this pass: the chunked-body cap bypass, the two download/pull
TOCTOU races, the 500-after-a-durable-save, the alias-blind detail
responses, the doubled tree walks, the swallowed LLM error bodies, the two
LM Studio vocabularies — plus the coverage the audit found missing (the
aiohttp adapter, the Civitai/lora-meta handler branches, llm-validate's
success path, template-tier aliases).

Everything is offline: urllib is canned, `server`/`folder_paths` are stubs,
and no test ever starts a real background thread.
"""

import asyncio
import hashlib
import io
import json
import os
import struct
import sys
import threading
import types
import urllib.error

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library, build_roots

from mrln import promptapi
from mrln.promptapi import core as core_mod
from mrln.promptapi import decompose as decompose_mod
from mrln.promptapi import library as library_mod
from mrln.promptapi import llm as llm_mod
from mrln.promptapi import lora as lora_mod
from mrln.promptapi import routes as routes_mod
from mrln.promptapi import settings as settings_mod
from mrln.promptlib import Library

# distinct from the AIR/model names other test files park in the shared
# status dicts — these are package singletons, not per-test state
AIR = "urn:air:sdxl:lora:civitai:4242@9001"
MODEL = "protocol-test-model:1b"

BLOB = b"MRLN protocol fixture payload " * 64
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_shared_state():
    """The status dicts, the hash cache and the secret list are ONE object
    each for the whole package (worker threads write them while poll
    handlers read them). Never rebind them — snapshot and restore in place."""
    snapshots = [
        (promptapi._LORA_DL_STATUS, dict(promptapi._LORA_DL_STATUS)),
        (promptapi._PULL_STATUS, dict(promptapi._PULL_STATUS)),
        (promptapi._HASH_CACHE, dict(promptapi._HASH_CACHE)),
    ]
    secrets = list(promptapi._KNOWN_SECRETS)
    promptapi._LORA_DL_STATUS.pop(AIR, None)
    promptapi._PULL_STATUS.pop(MODEL, None)
    yield
    for live, before in snapshots:
        live.clear()
        live.update(before)
    promptapi._KNOWN_SECRETS[:] = secrets


# -- shared stubs -------------------------------------------------------------


class _Canned:
    """Minimal urllib response stand-in: read()/read(n) plus headers."""

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


def _canned_urlopen(monkeypatch, result):
    """urlopen -> canned JSON (dict) or raise (Exception). Returns the log."""
    seen = []

    def urlopen(request, timeout=None):
        seen.append(request)
        if isinstance(result, Exception):
            raise result
        return _Canned(json.dumps(result).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return seen


def _fake_folder_paths(monkeypatch, root, installed=(), roots=None):
    module = types.SimpleNamespace(
        get_filename_list=lambda kind: list(installed),
        get_folder_paths=lambda kind: [str(root)] if roots is None else list(roots),
        get_full_path=lambda kind, name: str(root / name.replace("\\", "/")),
    )
    monkeypatch.setitem(sys.modules, "folder_paths", module)
    return module


def _no_threads(monkeypatch, module):
    """Replace one module's `threading` reference with a Thread that records
    instead of starting. The module-level Lock objects were built at import
    and are untouched, so the real claim-under-lock still runs."""
    spawned = []

    class _Recorder:
        def __init__(self, *, target=None, args=(), **kwargs):
            spawned.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(
        module,
        "threading",
        types.SimpleNamespace(Thread=_Recorder, Lock=threading.Lock, get_ident=threading.get_ident),
    )
    return spawned


def _write_safetensors(path, meta):
    header = json.dumps({"__metadata__": meta, "__dummy__": None}).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(header)) + header)
    return path


# -- package invariants -------------------------------------------------------


def test_package_singletons_are_one_object_each():
    """The split package shares mutable state BY REFERENCE across submodules;
    a rebind (or a re-created dict in a cleanup) would silently give worker
    threads and poll handlers two different objects."""
    assert promptapi._LORA_DL_STATUS is lora_mod._LORA_DL_STATUS
    assert promptapi._DL_LOCK is lora_mod._DL_LOCK
    assert promptapi._HASH_CACHE is lora_mod._HASH_CACHE
    assert promptapi._HASH_LOCKS is lora_mod._HASH_LOCKS
    assert promptapi._KNOWN_SECRETS is lora_mod._KNOWN_SECRETS
    assert promptapi._PULL_STATUS is llm_mod._PULL_STATUS
    assert promptapi._PULL_LOCK is llm_mod._PULL_LOCK
    assert promptapi._SETTINGS_LOCK is settings_mod._SETTINGS_LOCK


def test_monkeypatch_seams_stay_module_objects():
    """Tests and the node patch THROUGH these: `promptapi.json` is the
    documented seam for the atomic writer, and de-compose reaches llm_chat
    via the module object so it stays patchable."""
    assert promptapi.json is core_mod.json is routes_mod.json
    assert decompose_mod.llm is llm_mod
    assert library_mod.lora is lora_mod


def test_the_dead_public_settings_reader_is_gone():
    """read_settings() was a re-exported wrapper with no caller and no test."""
    assert not hasattr(promptapi, "read_settings")
    assert not hasattr(settings_mod, "read_settings")
    assert callable(settings_mod._read_settings)


# -- the aiohttp adapter (register_routes) ------------------------------------


class _FakeRoutes:
    """PromptServer.instance.routes: records what register_routes attaches."""

    def __init__(self):
        self.captured = {}

    def _capture(self, method, path):
        def wrap(endpoint):
            self.captured[(method, path)] = endpoint
            return endpoint

        return wrap

    def get(self, path):
        return self._capture("get", path)

    def post(self, path):
        return self._capture("post", path)


class _Stream:
    """aiohttp StreamReader stand-in: read(n) returns what is buffered NOW,
    never more, which is exactly why one read() cannot be trusted to deliver
    a whole chunked body."""

    def __init__(self, chunks=()):
        self._chunks = [bytes(c) for c in chunks]

    async def read(self, n=-1):
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if 0 <= n < len(chunk):
            self._chunks.insert(0, chunk[n:])
            return chunk[:n]
        return chunk


def _register(monkeypatch, tmp_path, table=None):
    pytest.importorskip("aiohttp")
    fake = _FakeRoutes()
    monkeypatch.setitem(
        sys.modules,
        "server",
        types.SimpleNamespace(
            PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=fake))
        ),
    )
    monkeypatch.setattr(routes_mod, "_warm_library_caches", lambda: None)
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))
    if table is not None:
        monkeypatch.setattr(routes_mod, "ROUTES", table)
    assert routes_mod.register_routes() is True
    return fake.captured


@pytest.fixture()
def adapter(tmp_path, monkeypatch):
    """One GET and one POST route wired to a spy handler, so the adapter's
    own guards can be asserted without a handler's semantics in the way."""
    calls = []

    def spy(library, payload):
        calls.append(payload)
        return 200, {"seen": payload}

    captured = _register(
        monkeypatch, tmp_path, (("get", "/spy", spy, False), ("post", "/spy", spy, True))
    )
    return types.SimpleNamespace(
        get=captured[("get", "/spy")], post=captured[("post", "/spy")], calls=calls
    )


def _mocked(method, path, *, chunks=(), headers=None):
    from aiohttp.test_utils import make_mocked_request

    return make_mocked_request(method, path, headers=headers, payload=_Stream(chunks))


def _drive(endpoint, request):
    response = asyncio.run(endpoint(request))
    return response.status, json.loads(response.body)


def test_register_routes_attaches_the_whole_table(tmp_path, monkeypatch):
    """The adapter was waived as 'verified during UAT'; it is testable
    headlessly, so nothing in it is unverified any more."""
    captured = _register(monkeypatch, tmp_path)
    assert len(captured) == len(promptapi.ROUTES)
    assert {(m, p) for m, p, _h, _b in promptapi.ROUTES} == set(captured)


def test_a_chunked_body_over_the_cap_is_refused(adapter):
    """`(request.content_length or 0) > MAX_BODY_BYTES` evaluated 0 > 1 MB for
    a body without Content-Length, so a chunked request walked straight into
    request.json() and buffered up to ComfyUI's ~100 MB upload cap."""
    oversized = b'{"pad": "' + b"x" * (promptapi.MAX_BODY_BYTES + 64) + b'"}'
    request = _mocked("POST", "/spy", chunks=[oversized])
    assert request.content_length is None  # the bypass precondition
    status, body = _drive(adapter.post, request)
    assert status == 413 and body["error"] == "request body too large"
    assert adapter.calls == []  # refused before the handler, and before parsing


def test_an_oversized_content_length_is_refused_without_reading(adapter):
    request = _mocked(
        "POST",
        "/spy",
        chunks=[b"{}"],
        headers={"Content-Length": str(promptapi.MAX_BODY_BYTES + 1)},
    )
    status, body = _drive(adapter.post, request)
    assert status == 413 and body["remediation"] == "send less data"
    assert adapter.calls == []


def test_a_body_split_across_reads_arrives_whole(adapter):
    """A single read() would hand back only the first buffered slice and turn
    a perfectly valid body into a 400."""
    payload = {"slug": "color", "pad": "y" * 4000}
    raw = json.dumps(payload).encode("utf-8")
    status, body = _drive(
        adapter.post,
        _mocked("POST", "/spy", chunks=[raw[i : i + 256] for i in range(0, len(raw), 256)]),
    )
    assert status == 200 and body["seen"] == payload
    assert adapter.calls == [payload]


def test_a_body_at_the_cap_still_passes(adapter):
    """The cap refuses ABOVE the limit — an exactly-1 MB body must survive."""
    pad = "z" * (promptapi.MAX_BODY_BYTES - len('{"pad": ""}'))
    raw = json.dumps({"pad": pad}).encode("utf-8")
    assert len(raw) == promptapi.MAX_BODY_BYTES
    status, body = _drive(adapter.post, _mocked("POST", "/spy", chunks=[raw]))
    assert status == 200 and body["seen"]["pad"] == pad


def test_non_json_and_non_object_bodies_are_400(adapter):
    status, body = _drive(adapter.post, _mocked("POST", "/spy", chunks=[b"{not json"]))
    assert status == 400 and body["error"] == "request body is not valid JSON"
    status, body = _drive(adapter.post, _mocked("POST", "/spy", chunks=[b"[1, 2]"]))
    assert status == 400 and body["error"] == "request body must be a JSON object"
    status, _ = _drive(adapter.post, _mocked("POST", "/spy"))  # empty body
    assert status == 400
    assert adapter.calls == []


def test_get_hands_the_handler_string_values_without_start(adapter):
    """Handlers see str-typed query values live but richer types in pytest —
    and 'start' is stripped so a bare cross-site GET can never write."""
    status, body = _drive(adapter.get, _mocked("GET", "/spy?air=urn%3Aair&seed=7&start=true"))
    assert status == 200
    assert body["seen"] == {"air": "urn:air", "seed": "7"}
    assert all(isinstance(v, str) for v in adapter.calls[0].values())


# -- TOCTOU: one claim per download / pull ------------------------------------


def test_a_second_download_start_mid_flight_never_replaces_the_claim(lib, tmp_path, monkeypatch):
    """Check-then-act with a folder_paths import and a settings read in
    between: two rapid POSTs both passed the guard, started two workers on ONE
    .part file, and the second replaced the status dict the first keeps
    writing (so the poll route watched an orphan). Re-entering through the
    settings read reproduces exactly that window, deterministically."""
    _fake_folder_paths(monkeypatch, tmp_path / "loras")
    spawned = _no_threads(monkeypatch, lora_mod)
    inner = {}

    def reentrant_read_settings(library):
        if not inner:
            inner["entered"] = True
            inner["body"] = promptapi.handle_lora_download(library, {"air": AIR, "start": True})
            inner["status"] = promptapi._LORA_DL_STATUS[AIR]
        return {}

    monkeypatch.setattr(lora_mod, "_read_settings", reentrant_read_settings)
    status, body = promptapi.handle_lora_download(lib, {"air": AIR, "start": True})

    assert status == 200 and body["status"] == "downloading"
    assert inner["body"][0] == 200
    assert promptapi._LORA_DL_STATUS[AIR] is inner["status"]  # never re-created
    assert body["loaded"] == 0 and body["detail"] == ""  # the running status, echoed
    assert len(spawned) == 1  # one worker, so one writer on the .part file


def test_a_second_pull_start_mid_gate_never_replaces_the_claim(lib, monkeypatch):
    """Same race on /llm-pull, where the loser would start a second 3600 s
    Ollama pull of the same model."""
    spawned = _no_threads(monkeypatch, llm_mod)
    real_backend_url = llm_mod.backend_url
    inner = {}

    def reentrant_backend_url(settings, key, default):
        if not inner:
            inner["entered"] = True
            inner["body"] = promptapi.handle_llm_pull(lib, {"model": MODEL, "start": True})
            inner["status"] = promptapi._PULL_STATUS[MODEL]
        return real_backend_url(settings, key, default)

    monkeypatch.setattr(llm_mod, "backend_url", reentrant_backend_url)
    status, body = promptapi.handle_llm_pull(lib, {"model": MODEL, "start": True})

    assert (status, body["detail"]) == (200, "already running")
    assert promptapi._PULL_STATUS[MODEL] is inner["status"]
    assert len(spawned) == 1


def test_download_part_files_are_per_start_unique(tmp_path, monkeypatch):
    """Two overlapping fetches of one AIR (the Composer's background download
    and the node's synchronous one) must not stream into a single '.part' —
    interleaved writes corrupt a file that is only caught later, and only when
    Civitai supplied a SHA256 to verify against."""
    dest = tmp_path / "loras"
    observed = []

    class _Watched(_Canned):
        def read(self, size=None):  # called while the .part file is open
            observed.extend(p.name for p in dest.glob("*.part*"))
            return super().read(size)

    def urlopen(request, timeout=None):
        if "/api/v1/model-versions/" in request.full_url:
            meta = {
                "files": [
                    {
                        "name": "kit.safetensors",
                        "primary": True,
                        "hashes": {"SHA256": BLOB_SHA},
                        "downloadUrl": "https://civitai.com/api/download/models/9001",
                    }
                ]
            }
            return _Canned(json.dumps(meta).encode("utf-8"))
        return _Watched(BLOB, {"Content-Length": str(len(BLOB))})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    status = {"status": "downloading", "detail": "", "loaded": 0, "total": 0}
    name = promptapi._fetch_lora_file({}, "", 9001, str(dest), "", status)

    assert name == "kit.safetensors"
    assert (dest / "kit.safetensors").read_bytes() == BLOB
    prefix = f"kit.safetensors.part-{os.getpid()}-"
    assert observed and all(n.startswith(prefix) for n in observed), observed
    assert list(dest.glob("*.part*")) == []  # replaced away, no torso left behind


# -- hashing: single flight + a free cache seed -------------------------------


def test_a_download_seeds_the_hash_cache_from_the_stream_it_verified(tmp_path, monkeypatch):
    """The fetch already SHA256s every byte to verify it; throwing that away
    made the Composer's next Civitai lookup re-read the whole file."""

    def urlopen(request, timeout=None):
        if "/api/v1/model-versions/" in request.full_url:
            meta = {"files": [{"name": "kit.safetensors", "primary": True, "hashes": {}}]}
            return _Canned(json.dumps(meta).encode("utf-8"))
        return _Canned(BLOB, {"Content-Length": str(len(BLOB))})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    dest = tmp_path / "loras"
    status = {"status": "downloading", "detail": "", "loaded": 0, "total": 0}
    promptapi._fetch_lora_file({}, "", 9001, str(dest), "", status)

    final = dest / "kit.safetensors"
    cached = promptapi._HASH_CACHE[promptapi._hash_key(final)]
    assert cached[1] == BLOB_SHA
    assert cached[0] == (os.stat(final).st_mtime_ns, os.stat(final).st_size)
    # …and the lookup path really answers from the cache: rewrite the bytes
    # while keeping (mtime_ns, size) identical — a re-read would hash '#'s
    stat = os.stat(final)
    final.write_bytes(b"#" * len(BLOB))
    os.utime(final, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert promptapi._sha256_of(final) == BLOB_SHA


def test_hashing_one_file_is_single_flight(tmp_path):
    """N clicks on one row used to mean N full multi-GB reads on N executor
    threads; now the extras wait for the first."""
    path = tmp_path / "kit.safetensors"
    path.write_bytes(BLOB)
    key = promptapi._hash_key(path)
    promptapi._HASH_CACHE.pop(key, None)
    result = []
    worker = threading.Thread(target=lambda: result.append(promptapi._sha256_of(path)))
    with lora_mod._hash_lock(key):  # stand in for a hash already in flight
        worker.start()
        worker.join(0.3)
        assert worker.is_alive() and result == []  # blocked, not hashing in parallel
    worker.join(5)
    assert result == [BLOB_SHA]
    assert promptapi._HASH_CACHE[key][1] == BLOB_SHA


def test_the_hash_cache_re_reads_a_changed_file(tmp_path):
    path = tmp_path / "kit.safetensors"
    path.write_bytes(BLOB)
    assert promptapi._sha256_of(path) == BLOB_SHA
    path.write_bytes(BLOB + b"!")
    assert promptapi._sha256_of(path) == hashlib.sha256(BLOB + b"!").hexdigest()


# -- error-body contract ------------------------------------------------------


def test_every_error_body_carries_both_contract_keys(lib):
    """The package promises {"error", "remediation"}; one 400 shipped without
    the remediation, so a panel rendering body.remediation showed 'undefined'."""
    for _method, path, handler, _reads_body in promptapi.ROUTES:
        status, body = handler(lib, {})
        if status >= 400:
            assert set(body) >= {"error", "remediation"}, (path, body)
            assert body["error"] and body["remediation"], (path, body)


def test_a_missing_loras_folder_names_its_remediation(lib, tmp_path, monkeypatch):
    _fake_folder_paths(monkeypatch, tmp_path, roots=[])
    status, body = promptapi.handle_lora_download(lib, {"air": AIR, "start": True})
    assert status == 400 and "loras folder" in body["error"]
    assert "extra_model_paths" in body["remediation"]


# -- Civitai lookup + lora-meta glue ------------------------------------------


def test_civitai_404_points_at_the_manual_catchword(lib, tmp_path, monkeypatch):
    root = tmp_path / "loras"
    root.mkdir()
    (root / "kit.safetensors").write_bytes(BLOB)
    _fake_folder_paths(monkeypatch, root, installed=["kit.safetensors"])
    _canned_urlopen(
        monkeypatch, urllib.error.HTTPError("https://civitai.com", 404, "Not Found", {}, None)
    )
    status, body = promptapi.handle_lora_civitai(lib, {"name": "kit.safetensors"})
    assert status == 404
    assert "is not on Civitai" in body["error"] and BLOB_SHA[:12] in body["error"]
    assert "catchword" in body["remediation"]


def test_civitai_other_http_codes_are_502_pointing_at_the_key(lib, tmp_path, monkeypatch):
    root = tmp_path / "loras"
    root.mkdir()
    (root / "kit.safetensors").write_bytes(BLOB)
    _fake_folder_paths(monkeypatch, root, installed=["kit.safetensors"])
    _canned_urlopen(
        monkeypatch, urllib.error.HTTPError("https://civitai.com", 500, "Boom", {}, None)
    )
    status, body = promptapi.handle_lora_civitai(lib, {"name": "kit.safetensors"})
    assert status == 502 and "HTTP 500" in body["error"]
    assert "API key" in body["remediation"]


def test_civitai_success_returns_the_summary_and_the_installed_casing(lib, tmp_path, monkeypatch):
    root = tmp_path / "loras"
    (root / "kits").mkdir(parents=True)
    (root / "kits" / "Hycade.safetensors").write_bytes(BLOB)
    _fake_folder_paths(monkeypatch, root, installed=["kits/Hycade.safetensors"])
    seen = _canned_urlopen(
        monkeypatch,
        {
            "id": 9001,
            "modelId": 4242,
            "name": "v1",
            "baseModel": "SDXL 1.0",
            "trainedWords": ["HycadeKit"],
            "model": {"name": "Hycade", "type": "LORA"},
        },
    )
    status, body = promptapi.handle_lora_civitai(lib, {"name": "kits\\hycade.safetensors"})
    assert status == 200
    assert body["trigger"] == "HycadeKit"
    assert body["air"] == "urn:air:sdxl:lora:civitai:4242@9001"
    assert body["name"] == "kits/Hycade.safetensors"  # the real casing, not the request
    assert BLOB_SHA in seen[0].full_url  # looked up by the file's own hash


def test_lora_meta_resolves_slash_and_case_mismatches(lib, tmp_path, monkeypatch):
    """'kits\\hycade.safetensors' (a backslash-authored library item on Linux)
    and 'kits/Hycade.safetensors' are the same file."""
    root = tmp_path / "loras"
    _write_safetensors(root / "kits" / "Hycade.safetensors", {"modelspec.trigger_phrase": "HYC"})
    _fake_folder_paths(monkeypatch, root, installed=["kits/Hycade.safetensors"])
    status, body = promptapi.handle_lora_meta(lib, {"name": "kits\\hycade.safetensors"})
    assert status == 200
    assert body == {
        "trigger": "HYC",
        "source": "modelspec.trigger_phrase",
        "name": "kits/Hycade.safetensors",
    }


def test_lora_meta_maps_unknown_triggerless_and_corrupt_files(lib, tmp_path, monkeypatch):
    root = tmp_path / "loras"
    _write_safetensors(root / "bare.safetensors", {})
    (root / "corrupt.safetensors").write_bytes(b"\x05\x00")
    _fake_folder_paths(monkeypatch, root, installed=["bare.safetensors", "corrupt.safetensors"])
    status, body = promptapi.handle_lora_meta(lib, {"name": "ghost.safetensors"})
    assert status == 404 and "not found in your loras folder" in body["error"]
    status, body = promptapi.handle_lora_meta(lib, {"name": "bare.safetensors"})
    assert status == 404 and "no trigger word" in body["error"]
    assert "ss_tag_frequency" in body["remediation"]
    status, body = promptapi.handle_lora_meta(lib, {"name": "corrupt.safetensors"})
    assert status == 400 and "catchword" in body["remediation"]


# -- LLM backends -------------------------------------------------------------


def _http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body))


def _chat(lib, **kwargs):
    return promptapi.llm_chat(
        lib,
        model=kwargs.pop("model", "gemma3:4b"),
        system="s",
        prompt="p",
        temperature=0.1,
        seed=1,
        max_tokens=16,
        timeout=5,
        **kwargs,
    )


def test_an_http_error_body_reaches_the_user(lib, monkeypatch):
    """urllib's str() is only 'HTTP Error 404: Not Found' and it leaves the
    body unread — the body is where the actionable reason lives."""
    detail = json.dumps({"error": "model 'gemma3:4b' not found, try pulling it first"})
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(
            _http_error(request.full_url, 404, detail.encode("utf-8"))
        ),
    )
    with pytest.raises(RuntimeError) as excinfo:
        _chat(lib, backend="ollama")
    message = str(excinfo.value)
    assert "HTTP 404" in message and "127.0.0.1:11434" in message
    assert "try pulling it first" in message
    assert excinfo.value.__cause__ is None  # no unscrubbed cause rides the log


def test_a_reflected_http_error_body_is_scrubbed_and_capped(lib, monkeypatch):
    """Same treatment every echoed string gets: the pull/download poll routes
    are unauthenticated, so nothing reflected may carry a credential."""
    key = "mrln-test-secret-9f3a71c40b"
    promptapi._remember_secret(key)
    body = f"rejected key {key} via ?token={key} ".encode() + b"z" * 400
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(
            _http_error(request.full_url, 401, body)
        ),
    )
    with pytest.raises(RuntimeError) as excinfo:
        _chat(lib, backend="ollama")
    message = str(excinfo.value)
    assert key not in message and "token=***" in message
    assert message.endswith("...") and len(message) < 300


def test_llm_validate_lists_models_and_filters_suggestions_by_stem(lib, monkeypatch):
    """The success branch drives every model dropdown; a broken stem filter
    re-suggests multi-GB downloads of an already-installed family."""
    _canned_urlopen(monkeypatch, {"models": [{"name": "gemma3:4b"}, {"name": "aaa-local:1b"}]})
    status, body = promptapi.handle_llm_validate(lib, {"provider": "ollama"})
    assert status == 200 and body["state"] == "ok"
    assert body["models"] == ["aaa-local:1b", "gemma3:4b"]  # sorted
    assert not [s for s in body["suggested"] if s.startswith("gemma3:")]  # stem filtered
    assert "qwen3:14b" in body["suggested"]


def test_llm_validate_reads_lmstudio_ids_and_offers_no_pulls(lib, monkeypatch):
    _canned_urlopen(monkeypatch, {"data": [{"id": "local-x"}, {"id": "local-a"}]})
    status, body = promptapi.handle_llm_validate(lib, {"provider": "lmstudio"})
    assert status == 200 and body["models"] == ["local-a", "local-x"]
    assert body["suggested"] == []  # LM Studio has no pull API


def test_both_lm_studio_spellings_work_at_every_entry_point(lib, monkeypatch):
    """The node's frozen enum says 'lm studio', llm-validate says 'lmstudio';
    a client that reused the validate vocabulary for /decompose used to get
    'unknown backend' dressed up as a backend failure."""
    _canned_urlopen(monkeypatch, {"data": [{"id": "local-x"}]})
    status, body = promptapi.handle_llm_validate(lib, {"provider": "lm studio"})
    assert status == 200 and body["provider"] == "lmstudio"  # canonicalized

    seen = {}

    def fake_post(url, payload, timeout, headers=None):
        seen["url"] = url
        return {"choices": [{"message": {"content": "rewritten"}}]}

    monkeypatch.setattr(llm_mod, "_post_json", fake_post)
    for spelling in promptapi.LMSTUDIO_SPELLINGS:
        assert _chat(lib, backend=spelling) == "rewritten"
        assert seen["url"].endswith("/v1/chat/completions")


def test_decompose_accepts_the_lmstudio_spelling(lib, monkeypatch):
    fragments = {"fragments": [{"text": "a bright red car", "section": "color", "item": "red"}]}

    def fake_post(url, payload, timeout, headers=None):
        return {"choices": [{"message": {"content": json.dumps(fragments)}}]}

    monkeypatch.setattr(llm_mod, "_post_json", fake_post)
    status, body = promptapi.handle_decompose(
        lib, {"prompt": "a bright red car", "engine": "llm", "backend": "lmstudio", "model": "m"}
    )
    assert status == 200 and "llm_error" not in body
    assert body["engine"] == "llm" and body["fragments"]


# -- de-compose timeout coercion ----------------------------------------------


@pytest.mark.parametrize("value", [90, 90.0, "90", 90.7])
def test_decompose_coerces_numeric_timeouts(lib, monkeypatch, value):
    """JSON serializers emit 90.0 for an integer; isinstance(int) refused it
    while every other handler coerces."""
    seen = {}

    def fake_chat(library, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("no backend in tests")

    monkeypatch.setattr(llm_mod, "llm_chat", fake_chat)
    status, body = promptapi.handle_decompose(
        lib, {"prompt": "a red car", "engine": "llm", "timeout": value}
    )
    assert status == 200 and "llm_error" in body  # rejected by the backend, not the parser
    assert seen["timeout"] == 90 and isinstance(seen["timeout"], int)


@pytest.mark.parametrize("value", [True, False, None, "soon", 2, 900, [90]])
def test_decompose_still_refuses_unusable_timeouts(lib, value):
    """bool is an int subclass, so `true` only failed the range check by luck."""
    status, body = promptapi.handle_decompose(
        lib, {"prompt": "a red car", "engine": "llm", "timeout": value}
    )
    assert status == 400 and "timeout" in body["error"]


# -- library detail: aliases, fingerprints, durable saves ---------------------


def _aliased_library(tmp_path):
    factory, user = build_roots(tmp_path)
    (factory / "aliases.json").write_text(
        json.dumps({"templates": {"retired": "basic"}, "sections": {"paint": "color"}}),
        encoding="utf-8",
    )
    return Library(factory, user)


def test_a_template_alias_resolves_through_raw_file_and_reports_the_live_slug(tmp_path):
    """Template-tier aliases had no test at all, and _raw_file re-implements
    the alias walk. tier_of() missed the alias, so the detail answered
    tier "" and echoed the dead slug a save-back would then resurrect."""
    lib = _aliased_library(tmp_path)
    assert lib.load_template("retired").slug == "basic"
    assert promptapi._raw_file(lib, "templates", "retired") == promptapi._raw_file(
        lib, "templates", "basic"
    )
    status, body = promptapi.handle_template(lib, {"slug": "retired"})
    assert status == 200
    assert (body["slug"], body["requested"]) == ("basic", "retired")
    assert body["tier"] == "factory"
    assert body["template"]["slug"] == "basic"
    assert body["raw"]["slots"][0]["id"] == "paint"


def test_a_section_alias_reports_the_live_slug_tier_and_item_origin(tmp_path):
    lib = _aliased_library(tmp_path)
    status, body = promptapi.handle_section(lib, {"slug": "paint"})
    assert status == 200
    assert (body["slug"], body["requested"]) == ("color", "paint")
    assert body["tier"] == "user"  # the fixture's user tier overrides color
    assert "" not in {item["origin"] for item in body["items"]}
    assert body["factory_raw"] is not None  # the extend-mode baseline, also alias-blind before


def _count_tree_walks(lib):
    """Count real rglob walks (scan-cache misses), not _scan() calls."""
    walks = []
    real = type(lib)._scan

    def counting(kind):
        if lib._scan_cache.get(kind) is None:
            walks.append(kind)
        return real(lib, kind)

    lib._scan = counting
    return walks


@pytest.mark.parametrize(
    "call",
    [
        lambda: ("handle_preview", {"template": "basic", "seed": 1}),
        lambda: ("handle_template", {"slug": "basic"}),
        lambda: ("handle_section", {"slug": "lighting"}),
    ],
)
def test_read_handlers_walk_each_kind_once(lib, call):
    """fingerprint() invalidates the memo and re-walks BOTH kind trees. Taken
    LAST that doubled every read handler's walks (and could label a stale
    payload with a fresh fingerprint); taken FIRST the walks are shared."""
    name, payload = call()
    walks = _count_tree_walks(lib)
    status, body = getattr(promptapi, name)(lib, payload)
    assert status == 200
    assert sorted(walks) == ["sections", "templates"]
    assert body["fingerprint"]


def test_save_section_stays_200_when_a_template_rewrite_fails(lib, monkeypatch):
    """The section file is already durably on disk when rename propagation
    runs: an unwritable user template (AV/sync lock on Windows) must not turn
    a landed save into a 500 the composer shows as 'save failed'."""
    assert (
        promptapi.handle_save_template(
            lib,
            {"slug": "mine", "data": {"slots": [{"id": "p", "ref": "color", "default": "red"}]}},
        )[0]
        == 200
    )
    real_replace = os.replace

    def flaky(src, dst, *args, **kwargs):
        if "templates" in str(dst):
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    status, body = promptapi.handle_save_section(
        lib,
        {
            "slug": "color",
            "data": {"items": [{"name": "crimson", "text": "bright red"}]},
            "renames": {"red": "crimson"},
        },
    )
    monkeypatch.undo()

    assert status == 200 and body["ok"] is True
    assert body["templates_rewritten"] == 0
    assert "mine" in body["rename_warning"] and "could not be re-pointed" in body["rename_warning"]
    # the save the client was told about really is on disk
    saved = json.loads((lib.user_root / "sections" / "color.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in saved["items"]] == ["crimson"]
    # and the template that could not be rewritten kept its old, valid content
    tpl = json.loads((lib.user_root / "templates" / "mine.json").read_text(encoding="utf-8"))
    assert tpl["slots"][0]["default"] == "red"
