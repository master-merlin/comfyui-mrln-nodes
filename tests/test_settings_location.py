"""Where pack-wide settings live, and what happens to an install that
updates across the move.

ECOSYSTEM S2 (user decision, 2026-08-16): `settings.json` holds LLM backends
and keys, the Civitai key, retention and thumbnail preferences — none of which
belong to the prompt domain, all of which every later domain reads. It moved
from `user/mrln/prompt/settings.json` to `user/mrln/settings.json`.

The migration has to be worth more than the tidiness, so it is held to three
promises here: an existing install keeps its keys the moment it updates, the
first save moves them, and reading never touches the disk (E7) — because a
read that silently rewrites a file holding API keys is exactly the behaviour
nobody can debug later.
"""

import json

import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln import promptapi, userdir
from mrln.promptapi import settings as settings_mod


def _pack_settings(lib):
    return lib.user_root.parent / "settings.json"


def _legacy_settings(lib):
    return lib.user_root / "settings.json"


def test_settings_are_written_at_the_pack_root_not_inside_the_prompt_domain(tmp_path):
    lib = build_library(tmp_path)
    status, _ = promptapi.handle_save_settings(lib, {"civitai_api_key": "key-abc"})
    assert status == 200
    assert _pack_settings(lib).is_file()
    assert not _legacy_settings(lib).exists()
    assert json.loads(_pack_settings(lib).read_text(encoding="utf-8"))["civitai_api_key"] == (
        "key-abc"
    )


def test_an_install_that_updates_still_finds_its_stored_keys(tmp_path):
    """The 0.1.1 layout: everything in the prompt folder, nothing at the root."""
    lib = build_library(tmp_path)
    lib.user_root.mkdir(parents=True, exist_ok=True)
    _legacy_settings(lib).write_text(
        json.dumps(
            {
                "civitai_api_key": "old-key",
                "llm_api_keys": {"anthropic": "old-anthropic"},
                "history_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    settings = settings_mod._read_settings(lib)
    assert settings["civitai_api_key"] == "old-key"
    assert settings["llm_api_keys"]["anthropic"] == "old-anthropic"

    status, body = promptapi.handle_settings(lib, {})
    assert status == 200
    assert body["civitai_key_set"] is True  # the handler sees it, and still never echoes it
    assert "old-key" not in json.dumps(body)


def test_reading_the_legacy_file_does_not_move_it(tmp_path):
    """E7: reading never mutates disk. A read that migrated would rewrite a
    file full of API keys on every listing request."""
    lib = build_library(tmp_path)
    lib.user_root.mkdir(parents=True, exist_ok=True)
    _legacy_settings(lib).write_text(json.dumps({"civitai_api_key": "old-key"}), encoding="utf-8")
    before = _legacy_settings(lib).stat().st_mtime_ns

    for _ in range(3):
        settings_mod._read_settings(lib)
        promptapi.handle_settings(lib, {})

    assert not _pack_settings(lib).exists()
    assert _legacy_settings(lib).stat().st_mtime_ns == before


def test_the_first_save_carries_the_whole_legacy_file_over(tmp_path):
    """Saving one field must not drop the others: the save is a read-modify-
    write, and the read is what picks the legacy file up."""
    lib = build_library(tmp_path)
    lib.user_root.mkdir(parents=True, exist_ok=True)
    _legacy_settings(lib).write_text(
        json.dumps(
            {
                "civitai_api_key": "old-key",
                "llm_api_keys": {"anthropic": "old-anthropic", "openai": "old-openai"},
                "history_months": 7,
            }
        ),
        encoding="utf-8",
    )
    status, _ = promptapi.handle_save_settings(lib, {"civitai_api_key": "new-key"})
    assert status == 200

    moved = json.loads(_pack_settings(lib).read_text(encoding="utf-8"))
    assert moved["civitai_api_key"] == "new-key"
    assert moved["llm_api_keys"] == {"anthropic": "old-anthropic", "openai": "old-openai"}
    assert moved["history_months"] == 7


def test_the_old_file_is_left_alone_rather_than_deleted(tmp_path):
    """It holds API keys. Deleting a user's secrets to tidy a path is not a
    trade this pack makes — and a downgrade back to 0.1.1 then still works."""
    lib = build_library(tmp_path)
    lib.user_root.mkdir(parents=True, exist_ok=True)
    _legacy_settings(lib).write_text(json.dumps({"civitai_api_key": "old-key"}), encoding="utf-8")
    promptapi.handle_save_settings(lib, {"civitai_api_key": "new-key"})
    assert _legacy_settings(lib).is_file()
    assert json.loads(_legacy_settings(lib).read_text(encoding="utf-8")) == {
        "civitai_api_key": "old-key"
    }


def test_the_new_file_wins_when_both_exist(tmp_path):
    lib = build_library(tmp_path)
    lib.user_root.mkdir(parents=True, exist_ok=True)
    _legacy_settings(lib).write_text(json.dumps({"civitai_api_key": "stale"}), encoding="utf-8")
    _pack_settings(lib).write_text(json.dumps({"civitai_api_key": "current"}), encoding="utf-8")
    assert settings_mod._read_settings(lib)["civitai_api_key"] == "current"


def test_a_corrupt_new_file_falls_back_rather_than_losing_the_old_keys(tmp_path):
    """Half-written JSON at the new path used to mean "no settings at all".
    Falling through to the legacy file keeps a working install working."""
    lib = build_library(tmp_path)
    lib.user_root.mkdir(parents=True, exist_ok=True)
    _legacy_settings(lib).write_text(json.dumps({"civitai_api_key": "old-key"}), encoding="utf-8")
    _pack_settings(lib).write_text('{"civitai_api_key": "trunc', encoding="utf-8")
    assert settings_mod._read_settings(lib)["civitai_api_key"] == "old-key"


def test_a_domain_root_of_none_has_no_settings_anywhere(tmp_path):
    """Headless with no user tier: no path to write to, and nothing raises."""
    assert userdir.pack_root(None) is None
    assert userdir.settings_path(None) is None
    assert userdir.legacy_settings_path(None) is None


def test_the_pack_root_is_derived_from_the_domain_root_not_the_environment(tmp_path):
    """MRLN_PROMPT_DIR redirects a domain root, and the pack root has to
    follow it — a root computed independently would write a real API key into
    the real ComfyUI user directory during a test run."""
    lib = build_library(tmp_path)
    assert userdir.pack_root(lib.user_root) == lib.user_root.parent
    assert tmp_path in userdir.settings_path(lib.user_root).parents
