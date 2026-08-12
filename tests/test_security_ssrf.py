"""SSRF hardening of the LLM backend URLs (spec 2.1).

The `ollama_url` / `lmstudio_url` settings are FETCHED BY THE SERVER, so an
arbitrary string stored there turns ComfyUI into a request proxy. Two gates,
both living in `settings._validate_backend_url` and applied at save time AND
at every fetch site (validate / pull / chat) — the second half matters because
a LAN URL written before this check existed is still sitting in someone's
settings.json. Everything here is offline: urllib is canned.
"""

import json

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln import promptapi
from mrln.promptapi import llm as api_llm
from mrln.promptapi import settings as api_settings

LAN_URL = "http://10.0.0.5:11434"


def _store_llm(lib, **llm):
    """Write settings.json directly — simulates a file written by an older
    version, bypassing every validation the save handler now performs."""
    lib.user_root.mkdir(parents=True, exist_ok=True)
    (lib.user_root / "settings.json").write_text(json.dumps({"llm": llm}), encoding="utf-8")


def _stored(lib):
    path = lib.user_root / "settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


class _CannedResponse:
    """urllib response stand-in: read() + context manager."""

    def __init__(self, body):
        self._body = body

    def read(self, size=None):
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _canned_urlopen(monkeypatch, body, log=None):
    def urlopen(request, timeout=None):
        if log is not None:
            log.append(getattr(request, "full_url", request))
        return _CannedResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)


# -- shape gate: only plain http(s) URLs are ever stored -----------------------


@pytest.mark.parametrize(
    "bad",
    [
        "file:///etc/passwd",  # local file read
        "gopher://evil.example:70/_dial",  # protocol smuggling
        "http://user:pass@evil.example",  # credentials ride along
        "10.0.0.5",  # no scheme at all
        "://nope",
        "http://",  # no host
        "https://#frag",
        "http://127.0.0.1:11434?x=1",  # query would corrupt the appended path
    ],
)
def test_save_rejects_non_http_urls(tmp_path, bad):
    lib = build_library(tmp_path)
    status, body = promptapi.handle_save_settings(lib, {"llm": {"ollama_url": bad}})
    assert status == 400, (bad, body)
    assert body["error"] and body["remediation"]
    assert bad not in json.dumps(_stored(lib))  # never stored, not even briefly


def test_invalid_url_aborts_the_whole_save(tmp_path):
    # a mixed payload must not half-apply: the valid half is dropped too
    lib = build_library(tmp_path)
    status, _body = promptapi.handle_save_settings(
        lib, {"civitai_api_key": "should-not-land", "llm": {"ollama_url": "file:///etc/passwd"}}
    )
    assert status == 400
    assert "should-not-land" not in json.dumps(_stored(lib))


def test_save_accepts_loopback(tmp_path):
    lib = build_library(tmp_path)
    status, _body = promptapi.handle_save_settings(
        lib,
        {"llm": {"ollama_url": "http://127.0.0.1:11434", "lmstudio_url": "http://localhost:1234/"}},
    )
    assert status == 200
    status, body = promptapi.handle_settings(lib, {})
    assert body["llm"]["ollama_url"] == "http://127.0.0.1:11434"
    assert body["llm"]["lmstudio_url"] == "http://localhost:1234"  # trailing slash stripped
    assert body["llm"]["allow_remote"] is False  # off unless the user says otherwise


def test_packaged_defaults_survive_the_gate():
    # the shipped defaults must pass validation untouched, or a fresh install
    # cannot talk to a local Ollama at all
    for url in (api_settings.DEFAULT_OLLAMA_URL, api_settings.DEFAULT_LMSTUDIO_URL):
        assert api_settings._validate_backend_url(url, key="llm.ollama_url") == url


def test_ipv6_loopback_is_recognised_without_brackets():
    # urlsplit() hands the host back bare, so ::1 must match unbracketed
    assert api_settings._is_loopback_host("::1")
    assert api_settings._is_loopback_host("[::1]")
    assert api_settings._is_loopback_host("127.0.0.2")  # all of 127/8 is us
    assert not api_settings._is_loopback_host("10.0.0.5")
    assert not api_settings._is_loopback_host("localhost.evil.example")
    assert api_settings._validate_backend_url("http://[::1]:11434", key="llm.ollama_url")


# -- reach gate: loopback only, unless llm.allow_remote --------------------------


def test_lan_url_rejected_at_save_with_actionable_remediation(tmp_path):
    lib = build_library(tmp_path)
    status, body = promptapi.handle_save_settings(lib, {"llm": {"ollama_url": LAN_URL}})
    assert status == 400
    assert "10.0.0.5" in body["error"] and "loopback" in body["error"]
    # the whole point: a user legitimately running Ollama on another box must
    # be told which switch unblocks them
    assert "allow_remote" in body["remediation"]
    assert LAN_URL not in json.dumps(_stored(lib))


def test_validate_refuses_stale_stored_lan_url(tmp_path):
    # settings.json written by an older build, never through the new gate
    lib = build_library(tmp_path)
    _store_llm(lib, ollama_url=LAN_URL)
    status, body = promptapi.handle_llm_validate(lib, {"provider": "ollama"})
    assert status == 400  # refused at USE time, no request goes out
    assert "10.0.0.5" in body["error"] and "allow_remote" in body["remediation"]


def test_pull_refuses_stale_stored_lan_url(tmp_path):
    lib = build_library(tmp_path)
    _store_llm(lib, ollama_url=LAN_URL)
    status, body = promptapi.handle_llm_pull(lib, {"model": "ssrf-pull", "start": True})
    assert status == 400 and "allow_remote" in body["remediation"]
    assert "ssrf-pull" not in promptapi._PULL_STATUS  # no worker thread started


def test_chat_refuses_stale_stored_lan_url(tmp_path):
    lib = build_library(tmp_path)
    _store_llm(lib, lmstudio_url=LAN_URL)
    with pytest.raises(RuntimeError, match="allow_remote"):
        promptapi.llm_chat(
            lib,
            backend="lm studio",
            model="local-model",
            system="s",
            prompt="p",
            temperature=0.2,
            seed=1,
            max_tokens=16,
            timeout=5,
        )


def test_allow_remote_true_permits_the_lan_backend(tmp_path, monkeypatch):
    lib = build_library(tmp_path)
    status, _body = promptapi.handle_save_settings(
        lib, {"llm": {"allow_remote": True, "ollama_url": LAN_URL}}
    )
    assert status == 200  # flag and URL may land in the SAME save
    assert promptapi.handle_settings(lib, {})[1]["llm"]["allow_remote"] is True

    log = []
    _canned_urlopen(monkeypatch, {"models": [{"name": "gemma3:4b"}]}, log)
    status, body = promptapi.handle_llm_validate(lib, {"provider": "ollama"})
    assert status == 200 and body["models"] == ["gemma3:4b"]
    assert log == [f"{LAN_URL}/api/tags"]


def test_allow_remote_must_be_a_bool(tmp_path):
    lib = build_library(tmp_path)
    assert promptapi.handle_save_settings(lib, {"llm": {"allow_remote": "yes"}})[0] == 400
    assert "allow_remote" not in json.dumps(_stored(lib))


def test_turning_allow_remote_off_leaves_use_time_enforcement(tmp_path):
    lib = build_library(tmp_path)
    assert (
        promptapi.handle_save_settings(lib, {"llm": {"allow_remote": True, "ollama_url": LAN_URL}})[
            0
        ]
        == 200
    )
    # tightening the flag is always allowed; the stored URL stays visible in
    # the settings echo so the user can fix it, but is refused when used
    assert promptapi.handle_save_settings(lib, {"llm": {"allow_remote": False}})[0] == 200
    body = promptapi.handle_settings(lib, {})[1]
    assert body["llm"]["ollama_url"] == LAN_URL and body["llm"]["allow_remote"] is False
    assert promptapi.handle_llm_validate(lib, {"provider": "ollama"})[0] == 400


# -- reflected error strings stay diagnostic, never a response body -------------


def test_unreachable_error_does_not_echo_a_response_body(tmp_path, monkeypatch):
    lib = build_library(tmp_path)

    def urlopen(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    status, body = promptapi.handle_llm_validate(lib, {"provider": "ollama"})
    assert status == 502
    assert "unreachable" in body["error"] and "OSError" in body["error"]


def test_exc_detail_caps_a_reflected_response_body():
    long_body = "<html>\n" + "SECRET-PAGE " * 500 + "</html>"
    detail = api_llm._exc_detail(RuntimeError(long_body))
    assert detail.startswith("RuntimeError: ") and detail.endswith("...")
    assert len(detail) < 260 and "\n" not in detail  # capped and collapsed
    assert "SECRET-PAGE" in detail  # still useful for debugging
    assert api_llm._exc_detail(TimeoutError()) == "TimeoutError"


def test_pull_worker_detail_is_class_and_message(tmp_path, monkeypatch):
    def urlopen(request, timeout=None):
        raise OSError("connection refused by " + "x" * 500)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    promptapi._pull_worker("http://127.0.0.1:9", "ssrf-worker")
    detail = promptapi._PULL_STATUS.pop("ssrf-worker")["detail"]
    assert detail.startswith("OSError: connection refused by") and len(detail) < 260
