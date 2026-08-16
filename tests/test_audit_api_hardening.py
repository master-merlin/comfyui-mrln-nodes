"""Audit-hardening regressions (pre-release fixes): GET never mutates
state, failed downloads leave no .part torsos behind, settings/profile/
template persistence is atomic, and the LoRA download worker's integrity,
auth and heal mechanics hold — all offline (urllib is canned)."""

import hashlib
import json

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln import promptapi
from mrln.promptlib import Library

AIR = "urn:air:sdxl:lora:civitai:333@444"


# -- state-changing GET is dead: only JSON true starts -------------------------


def test_lora_download_start_needs_json_true(tmp_path):
    lib = Library(tmp_path / "factory", tmp_path / "user")
    air = "urn:air:sdxl:lora:civitai:5@6"
    # GET payloads are query-string dicts — every value a string, and NO
    # string may start a download (?start=1 or even ?start=false only polls)
    for value in ("1", "true", "True", "false", "0"):
        status, body = promptapi.handle_lora_download(lib, {"air": air, "start": value})
        assert (status, body["status"]) == (200, "unknown"), value
    assert air not in promptapi._LORA_DL_STATUS
    # JSON true (a POST body) still reaches the start branch
    status, body = promptapi.handle_lora_download(lib, {"air": air, "start": True})
    assert status == 400 and "ComfyUI" in body["error"]


def test_llm_pull_start_needs_json_true(tmp_path):
    lib = build_library(tmp_path)
    for value in ("1", "true", "false"):
        status, body = promptapi.handle_llm_pull(lib, {"model": "audit-model", "start": value})
        assert (status, body["status"]) == (200, "unknown"), value
    assert "audit-model" not in promptapi._PULL_STATUS


# -- download worker (canned urllib: no network, no ComfyUI) -------------------

BLOB = b"MRLN fake safetensors payload " * 64
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()


class _CannedResponse:
    """Minimal stand-in for urllib's response: read()/read(n) + headers."""

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


class _BrokenStream:
    """One good chunk, then the connection dies mid-stream."""

    def __init__(self):
        self._sent = False
        self.headers = {"Content-Length": str(1 << 30)}

    def read(self, size=None):
        if self._sent:
            raise OSError("connection reset mid-stream")
        self._sent = True
        return b"x" * 1024

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _meta(sha=BLOB_SHA):
    """Version metadata: a zip and a secondary BEFORE the primary — the
    worker must filter to .safetensors and pick the primary file."""
    return {
        "files": [
            {"name": "training-data.zip", "downloadUrl": "https://civitai.com/x/zip"},
            {"name": "secondary.safetensors", "downloadUrl": "https://civitai.com/x/second"},
            {
                "name": "bmw_m4_cs.safetensors",
                "primary": True,
                "hashes": {"SHA256": sha.upper()},
                "downloadUrl": "https://civitai.com/api/download/models/444",
            },
        ]
    }


def _run_worker(
    tmp_path, monkeypatch, *, meta=None, download=BLOB, token="", filename="", heal=None
):
    """Call the worker synchronously with urllib canned; returns
    (status, request_log, dest_dir)."""
    meta = _meta() if meta is None else meta
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        if "/api/v1/model-versions/" in request.full_url:
            if isinstance(meta, Exception):
                raise meta
            return _CannedResponse(json.dumps(meta).encode("utf-8"))
        if isinstance(download, bytes):
            return _CannedResponse(download, {"Content-Length": str(len(download))})
        return download

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
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
    promptapi._lora_download_worker(AIR, meta_headers, token, 444, str(dest), filename, heal)
    return promptapi._LORA_DL_STATUS[AIR], calls, dest


def test_worker_picks_primary_and_keeps_the_token_out_of_the_url(tmp_path, monkeypatch):
    status, calls, dest = _run_worker(tmp_path, monkeypatch, token="tok-SECRET-123")
    assert status["status"] == "done", status
    assert status["name"] == "bmw_m4_cs.safetensors"  # primary pick, not files[0]
    assert (dest / "bmw_m4_cs.safetensors").read_bytes() == BLOB
    assert list(dest.glob("*.part")) == []
    meta_req, dl_req = calls
    assert "/api/v1/model-versions/444" in meta_req.full_url
    assert meta_req.has_header("Authorization")  # metadata may use the header
    assert dl_req.full_url.startswith("https://civitai.com/api/download/models/444")
    # The key now rides a header so it can never reach a reflected URL string.
    # The query form survives only as the 401/403 presigned-CDN fallback —
    # covered in tests/test_security_secrets.py.
    assert dl_req.get_header("Authorization") == "Bearer tok-SECRET-123"
    assert "token=" not in dl_req.full_url
    assert "tok-SECRET-123" not in json.dumps(status)  # the key is never echoed


def test_worker_sha_mismatch_discards_file(tmp_path, monkeypatch):
    status, _calls, dest = _run_worker(tmp_path, monkeypatch, meta=_meta(sha="0" * 64))
    assert status["status"] == "error" and "SHA256 mismatch" in status["detail"]
    assert list(dest.iterdir()) == []  # neither the final file nor a .part remains


def test_worker_failure_mid_stream_leaves_no_part_file(tmp_path, monkeypatch):
    status, _calls, dest = _run_worker(tmp_path, monkeypatch, download=_BrokenStream())
    assert status["status"] == "error" and "connection reset" in status["detail"]
    assert list(dest.iterdir()) == []  # the .part torso was cleaned up


def test_worker_metadata_failure_never_raises(tmp_path, monkeypatch):
    status, _calls, dest = _run_worker(tmp_path, monkeypatch, meta=RuntimeError("civitai is down"))
    assert status["status"] == "error" and "civitai is down" in status["detail"]
    assert not dest.exists()  # failed before anything touched the disk


@pytest.fixture()
def lora_lib(tmp_path):
    factory = tmp_path / "factory"
    path = factory / "sections" / "lora" / "car.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    item = {
        "name": "m4-kit",
        "text": "BMWM4CS_G82",
        "data": {"lora": "mastermerlin\\bmw_m4_cs.safetensors", "strength_model": 0.35},
    }
    path.write_text(json.dumps({"items": [item]}), encoding="utf-8")
    return Library(factory, tmp_path / "user")


def test_worker_heals_section_when_path_changed(lora_lib, tmp_path, monkeypatch):
    heal = (lora_lib, "lora/car", "m4-kit", "kits", "mastermerlin\\bmw_m4_cs.safetensors")
    status, _calls, _dest = _run_worker(
        tmp_path, monkeypatch, filename="bmw_m4_cs.safetensors", heal=heal
    )
    assert status["status"] == "done"
    assert status["healed"] == "kits/bmw_m4_cs.safetensors"
    item = next(i for i in lora_lib.load_section("lora/car").items if i.name == "m4-kit")
    assert item.data["lora"] == "kits/bmw_m4_cs.safetensors"
    assert item.text == "BMWM4CS_G82"  # heal snapshots the item, texts survive


def test_worker_heal_skipped_when_stored_path_equal(lora_lib, tmp_path, monkeypatch):
    # stored differs only by slash flavor and case -> equal -> no pointless
    # user-tier snapshot is written
    heal = (lora_lib, "lora/car", "m4-kit", "kits", "kits\\BMW_M4_CS.safetensors")
    status, _calls, _dest = _run_worker(
        tmp_path, monkeypatch, filename="bmw_m4_cs.safetensors", heal=heal
    )
    assert status["status"] == "done"
    assert "healed" not in status
    assert not (tmp_path / "user" / "sections" / "lora" / "car.json").exists()


# -- atomic persistence (tmp + os.replace, the save_user pattern) --------------


def test_save_settings_atomic_and_crash_safe(tmp_path, monkeypatch):
    lib = build_library(tmp_path)
    assert promptapi.handle_save_settings(lib, {"civitai_api_key": "first-key"})[0] == 200
    # pack-wide settings sit one level up, at user/mrln/settings.json (S2)
    settings_file = lib.user_root.parent / "settings.json"
    assert list(settings_file.parent.glob("*.tmp")) == []  # the tmp was replaced away

    def boom(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(promptapi.json, "dump", boom)
    status, body = promptapi.handle_save_settings(lib, {"civitai_api_key": "second-key"})
    assert status == 500
    assert "first-key" not in json.dumps(body) and "second-key" not in json.dumps(body)
    monkeypatch.undo()
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    assert settings["civitai_api_key"] == "first-key"  # the live file never truncates


def test_save_profile_write_is_atomic(tmp_path):
    lib = build_library(tmp_path)
    status, body = promptapi.handle_save_profile(lib, {"name": "krea", "data": {"label": "K"}})
    assert status == 200, body
    assert list(lib.user_root.glob("*.tmp")) == []
    data = json.loads((lib.user_root / "profiles.json").read_text(encoding="utf-8"))
    assert data["profiles"]["krea"] == {"label": "K"}


def test_rename_rewrite_is_atomic(tmp_path):
    lib = build_library(tmp_path)
    tpl = {"slots": [{"id": "paint", "ref": "color", "default": "red"}]}
    assert promptapi.handle_save_template(lib, {"slug": "mine", "data": tpl})[0] == 200
    status, body = promptapi.handle_save_section(
        lib,
        {
            "slug": "color",
            "data": {"items": [{"name": "crimson", "text": "bright red"}]},
            "renames": {"red": "crimson"},
        },
    )
    assert status == 200 and body["templates_rewritten"] == 1
    raw = json.loads((lib.user_root / "templates" / "mine.json").read_text(encoding="utf-8"))
    assert raw["slots"][0]["default"] == "crimson"
    assert list((lib.user_root / "templates").glob("*.tmp")) == []
