"""Multiple trigger words per LoRA (SPEC 4.3).

The whole design fits in two sentences, and every test below is a proof of
one of them:

  `data.lora_info.trained_words` is PROVENANCE — the full list the source
  (Civitai / safetensors metadata) gave us. Mute and solo never edit it.

  The item's TEXT is the catchword, and it is TRUTH — the words that actually
  render, joined ", " in trained_words order. Mute = present in provenance,
  absent from the catchword. Solo = mute all the others, which collapses to
  the same persisted state.

So the editor's chip state is a set difference over two fields that already
exist: nothing is widget-only, and a reload re-derives it from the FILE. The
back-compat guarantee (default selection = the first word) is what keeps every
item written before this change rendering byte-identically.
"""

import hashlib
import json
import struct
import sys
import types
import urllib.request

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln import promptapi
from mrln import promptlib as pl

WORDS = ["CarKit", "wide body", "carbon fibre"]
AIR = "urn:air:flux1:lora:civitai:123@789"
BLOB = b"weights" * 512
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()

VERSION_RESPONSE = {
    "id": 789,
    "modelId": 123,
    "baseModel": "Flux.1 D",
    "trainedWords": [" CarKit ", "wide body", "carbon fibre", "  "],
    "name": "v2.0",
    "model": {"name": "CarKit", "type": "LORA"},
}


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


# -- the summary: every word, not just the first -------------------------------


def test_the_summary_returns_every_trained_word():
    out = promptapi._civitai_summary(VERSION_RESPONSE)
    assert out["trained_words"] == WORDS  # trimmed, blanks dropped, order kept
    assert out["trigger"] == "CarKit"  # the old single-word field is unchanged
    assert out["air"] == AIR


def test_the_summary_of_a_wordless_version_is_empty_not_none():
    out = promptapi._civitai_summary({"id": 1, "modelId": 2, "trainedWords": []})
    assert out["trained_words"] == [] and out["trigger"] is None
    assert promptapi.lora_info(out) == {"air": out["air"]}


# -- provenance vs truth: the pure derivation ---------------------------------


def test_the_default_selection_is_the_first_word_and_nothing_else():
    """Back-compat is non-negotiable: this is exactly what the Civitai lookup
    has always written into a new item's catchword."""
    assert promptapi.default_trigger_selection(WORDS) == ["CarKit"]
    assert promptapi.render_catchword(WORDS, promptapi.default_trigger_selection(WORDS)) == "CarKit"
    assert promptapi.default_trigger_selection([]) == []
    assert promptapi.render_catchword([], []) == ""


def test_a_multi_selection_renders_in_provenance_order():
    """Order comes from trained_words, never from the click order — that is
    what makes a selection deterministic across machines."""
    assert promptapi.render_catchword(WORDS, ["carbon fibre", "CarKit"]) == "CarKit, carbon fibre"
    assert promptapi.render_catchword(WORDS, WORDS) == "CarKit, wide body, carbon fibre"
    assert promptapi.render_catchword(WORDS, reversed(WORDS)) == "CarKit, wide body, carbon fibre"


def test_all_muted_renders_nothing():
    """Legal state: a LoRA whose trigger is baked in, or simply unwanted."""
    assert promptapi.render_catchword(WORDS, []) == ""
    state = promptapi.trigger_selection(WORDS, "")
    assert state["active"] == [] and state["muted"] == WORDS and state["extra"] == []


def test_mute_is_absence_and_solo_collapses_to_the_same_state():
    solo = promptapi.render_catchword(WORDS, ["wide body"])
    mute_the_rest = promptapi.render_catchword(WORDS, [w for w in WORDS if w != "CarKit"])
    assert solo == "wide body"
    assert mute_the_rest == "wide body, carbon fibre"
    # solo'ing one word and muting every other word are the same operation
    assert solo == promptapi.render_catchword(WORDS, [w for w in WORDS if w == "wide body"])
    state = promptapi.trigger_selection(WORDS, solo)
    assert state["active"] == ["wide body"]
    assert state["muted"] == ["CarKit", "carbon fibre"]


def test_the_state_is_a_set_difference_over_the_two_stored_fields():
    state = promptapi.trigger_selection(WORDS, "CarKit, carbon fibre, my own phrase")
    assert state["words"] == WORDS
    assert state["active"] == ["CarKit", "carbon fibre"]
    assert state["muted"] == ["wide body"]
    assert state["extra"] == ["my own phrase"]  # user-added, no provenance entry


def test_free_text_words_are_kept_and_appended_after_the_known_ones():
    rendered = promptapi.render_catchword(WORDS, ["my own phrase", "CarKit"])
    assert rendered == "CarKit, my own phrase"
    assert promptapi.trigger_selection(WORDS, rendered)["extra"] == ["my own phrase"]


def test_selection_matching_ignores_case_but_renders_the_provenance_spelling():
    assert promptapi.render_catchword(WORDS, ["carkit"]) == "CarKit"
    assert promptapi.trigger_selection(WORDS, "carkit")["active"] == ["CarKit"]
    assert promptapi.trigger_selection(WORDS, "carkit")["extra"] == []


def test_the_catchword_splitter_tolerates_spacing_and_empties():
    assert promptapi.split_catchword(" a ,  b ,, c ") == ["a", "b", "c"]
    assert promptapi.split_catchword("") == []
    assert promptapi.split_catchword(None) == []


def test_lora_info_only_carries_what_the_source_actually_said():
    assert promptapi.lora_info({}) == {}
    assert promptapi.lora_info({"trained_words": ["  ", ""]}) == {}
    assert promptapi.lora_info({"trained_words": WORDS}, filename="kits/CarKit.safetensors") == {
        "trained_words": WORDS,
        "file": "kits/CarKit.safetensors",
    }


# -- round trip through a real file -------------------------------------------


def write_lora_section(lib, *, text, trained_words):
    raw = {
        "version": 1,
        "label": "Kits",
        "items": [
            {
                "name": "carkit",
                "text": text,
                "data": {
                    "lora": "kits/CarKit.safetensors",
                    "comment": AIR,
                    "lora_info": {"trained_words": list(trained_words)},
                },
            }
        ],
    }
    lib.save_user("sections", "lora/kits", raw)
    return raw


def reload_state(lib):
    item = next(i for i in lib.load_section("lora/kits").items if i.name == "carkit")
    return item, promptapi.trigger_selection(item.data["lora_info"]["trained_words"], item.text)


def test_mute_solo_round_trips_through_save_reload_rederive(lib):
    """The persistence-hardening lesson applied: the FILE is the truth, so the
    editor re-derives its chips instead of restoring widget state."""
    write_lora_section(lib, text="CarKit", trained_words=WORDS)
    item, state = reload_state(lib)
    assert state["active"] == ["CarKit"] and state["muted"] == ["wide body", "carbon fibre"]

    # the editor solos 'wide body' -> the only thing written is the catchword
    new_text = promptapi.render_catchword(WORDS, ["wide body"])
    write_lora_section(lib, text=new_text, trained_words=WORDS)
    item, state = reload_state(lib)
    assert item.text == "wide body"
    assert state["active"] == ["wide body"]
    assert state["muted"] == ["CarKit", "carbon fibre"]
    # provenance is untouched by any of it
    assert item.data["lora_info"]["trained_words"] == WORDS


def test_a_user_added_word_survives_a_reload(lib):
    text = promptapi.render_catchword(WORDS, ["CarKit", "shot on Portra"])
    write_lora_section(lib, text=text, trained_words=WORDS)
    item, state = reload_state(lib)
    assert item.text == "CarKit, shot on Portra"
    assert state["extra"] == ["shot on Portra"]
    assert state["active"] == ["CarKit"]


def test_an_all_muted_item_is_still_a_valid_library_item(lib):
    """All-muted renders no trigger text. The item's text may not be empty
    (schema), so an all-muted LoRA parks a single space-free marker — here the
    caller keeps the file name as the text and renders nothing by muting the
    slot instead. What matters: the derivation says 'nothing active'."""
    state = promptapi.trigger_selection(WORDS, "")
    assert state["active"] == [] and state["catchword"] == ""


# -- back-compat: an untouched item renders byte-identically -------------------


def build_lora_template(lib):
    lib.save_user(
        "templates",
        "lora/shot",
        {
            "version": 1,
            "label": "Shot",
            "prefix": "photo of a car",
            "slots": [{"id": "kit", "ref": "lora/kits", "default": "carkit"}],
        },
    )
    return lib.load_template("lora/shot")


def render_positive(lib):
    tpl = lib.load_template("lora/shot")
    composed = pl.compose(lib, tpl, seed=7, mode="as configured", selection={}, variables={})
    return composed.rendered.positive, pl.lora_entries(composed.resolved)


def test_adding_provenance_changes_nothing_that_renders(lib):
    """The proof that the design needs no new render code: `trained_words` is
    metadata, the catchword is the text, and only the text renders."""
    lib.save_user(
        "sections",
        "lora/kits",
        {
            "version": 1,
            "items": [
                {
                    "name": "carkit",
                    "text": "CarKit",
                    "data": {"lora": "kits/CarKit.safetensors", "comment": AIR},
                }
            ],
        },
    )
    build_lora_template(lib)
    before, before_loras = render_positive(lib)

    write_lora_section(lib, text="CarKit", trained_words=WORDS)
    after, after_loras = render_positive(lib)
    assert after == before == "photo of a car, CarKit"
    assert after_loras == before_loras


def test_selecting_more_words_is_what_changes_the_render(lib):
    write_lora_section(lib, text="CarKit", trained_words=WORDS)
    build_lora_template(lib)
    assert render_positive(lib)[0] == "photo of a car, CarKit"

    write_lora_section(lib, text=promptapi.render_catchword(WORDS, WORDS), trained_words=WORDS)
    assert render_positive(lib)[0] == "photo of a car, CarKit, wide body, carbon fibre"


# -- the two lookup endpoints answer with provenance AND truth -----------------


def fake_folder_paths(monkeypatch, installed, roots=("/loras",)):
    module = types.SimpleNamespace(
        get_filename_list=lambda kind: list(installed),
        get_folder_paths=lambda kind: list(roots),
        get_full_path=lambda kind, name: str(installed[name]),
    )
    monkeypatch.setitem(sys.modules, "folder_paths", module)
    return module


class _Canned:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self, size=None):
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_civitai_lookup_answers_with_all_words_and_the_default_catchword(
    lib, tmp_path, monkeypatch
):
    path = tmp_path / "carkit.safetensors"
    header = json.dumps({"__metadata__": {}}).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(header)) + header)
    fake_folder_paths(monkeypatch, {"kits/CarKit.safetensors": path})
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=None: _Canned(json.dumps(VERSION_RESPONSE).encode("utf-8")),
    )
    status, body = promptapi.handle_lora_civitai(lib, {"name": "kits/carkit.safetensors"})
    assert status == 200, body
    assert body["trained_words"] == WORDS
    assert body["trigger"] == "CarKit"
    assert body["catchword"] == "CarKit"  # truth defaults to the first word
    assert body["lora_info"]["trained_words"] == WORDS
    assert body["lora_info"]["air"] == AIR
    assert body["lora_info"]["file"] == "kits/CarKit.safetensors"


def test_the_metadata_lookup_stays_single_worded(tmp_path, monkeypatch):
    """safetensors metadata carries ONE trigger phrase, so this endpoint keeps
    its frozen single-word body; the client derives the (one-entry) provenance
    list from it. Only the Civitai lookup really has several words to give."""
    header = json.dumps({"__metadata__": {"modelspec.trigger_phrase": "CarKit"}}).encode("utf-8")
    path = tmp_path / "carkit.safetensors"
    path.write_bytes(struct.pack("<Q", len(header)) + header)
    fake_folder_paths(monkeypatch, {"kits/CarKit.safetensors": path})
    status, body = promptapi.handle_lora_meta(None, {"name": "kits/carkit.safetensors"})
    assert status == 200, body
    assert body == {
        "trigger": "CarKit",
        "source": "modelspec.trigger_phrase",
        "name": "kits/CarKit.safetensors",
    }
    derived = promptapi.lora_info({"trained_words": [body["trigger"]]}, filename=body["name"])
    assert derived["trained_words"] == ["CarKit"]


# -- provenance is stored on download -----------------------------------------


DOWNLOAD_FILES = [
    {
        "name": "carkit.safetensors",
        "primary": True,
        "hashes": {"SHA256": BLOB_SHA},
        "downloadUrl": "https://civitai.com/api/download/models/789",
    }
]


def download_meta(bare=False):
    """The Civitai model-version response the download already fetches — the
    same one that names the file also names every trained word. `bare` is a
    version that states nothing at all (no ids, so not even an AIR)."""
    if bare:
        return {"files": DOWNLOAD_FILES}
    return {**VERSION_RESPONSE, "files": DOWNLOAD_FILES}


@pytest.fixture()
def status_slot():
    """The download-status map is ONE module-level dict for the package; leave
    it exactly as it was found."""
    key = AIR
    promptapi._LORA_DL_STATUS[key] = {
        "status": "downloading",
        "detail": "",
        "loaded": 0,
        "total": 0,
    }
    yield key
    promptapi._LORA_DL_STATUS.pop(key, None)


def run_worker(lib, tmp_path, monkeypatch, status_key, *, meta, stored):
    def urlopen(request, timeout=None):
        if "/api/v1/model-versions/" in request.full_url:
            return _Canned(json.dumps(meta).encode("utf-8"))
        return _Canned(BLOB, {"Content-Length": str(len(BLOB))})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    heal = (lib, "lora/kits", "carkit", "", stored)
    promptapi._lora_download_worker(
        status_key, {"User-Agent": "x"}, "", 789, str(tmp_path / "loras"), "", heal
    )
    return promptapi._LORA_DL_STATUS[status_key]


def test_a_download_records_the_trained_words_on_the_item(lib, tmp_path, monkeypatch, status_slot):
    lib.save_user(
        "sections",
        "lora/kits",
        {"version": 1, "items": [{"name": "carkit", "text": "CarKit", "data": {"lora": "old.st"}}]},
    )
    status = run_worker(
        lib, tmp_path, monkeypatch, status_slot, meta=download_meta(), stored="old.st"
    )
    assert status["status"] == "done", status
    assert status["healed"] == "carkit.safetensors"
    item = next(i for i in lib.load_section("lora/kits").items if i.name == "carkit")
    assert item.data["lora"] == "carkit.safetensors"
    assert item.data["lora_info"]["trained_words"] == WORDS
    assert item.data["lora_info"]["air"] == AIR
    # the catchword the user curated is NEVER overwritten by a refresh
    assert item.text == "CarKit"


def test_provenance_is_recorded_even_when_the_path_did_not_move(
    lib, tmp_path, monkeypatch, status_slot
):
    lib.save_user(
        "sections",
        "lora/kits",
        {
            "version": 1,
            "items": [
                {"name": "carkit", "text": "wide body", "data": {"lora": "carkit.safetensors"}}
            ],
        },
    )
    status = run_worker(
        lib, tmp_path, monkeypatch, status_slot, meta=download_meta(), stored="carkit.safetensors"
    )
    assert status["status"] == "done" and "healed" not in status
    item = next(i for i in lib.load_section("lora/kits").items if i.name == "carkit")
    assert item.data["lora_info"]["trained_words"] == WORDS
    assert item.text == "wide body"  # a curated selection survives the refresh
    assert promptapi.trigger_selection(WORDS, item.text)["muted"] == ["CarKit", "carbon fibre"]


def test_a_version_that_states_nothing_writes_no_pointless_snapshot(
    lib, tmp_path, monkeypatch, status_slot
):
    """No provenance and no move means nothing to say — and no user-tier
    write, which is the behavior the pre-existing 'path unchanged' test pins."""
    (lib.user_root / "sections" / "lora").mkdir(parents=True, exist_ok=True)
    (lib.user_root / "sections" / "lora" / "kits.json").unlink(missing_ok=True)
    status = run_worker(
        lib,
        tmp_path,
        monkeypatch,
        status_slot,
        meta=download_meta(bare=True),
        stored="carkit.safetensors",
    )
    assert status["status"] == "done" and "lora_info" not in status
    assert not (lib.user_root / "sections" / "lora" / "kits.json").exists()


def test_the_status_map_never_leaks_a_secret_through_the_new_field(
    lib, tmp_path, monkeypatch, status_slot
):
    promptapi.handle_save_settings(lib, {"civitai_api_key": "civ-SECRET-abcdef"})
    status = run_worker(
        lib, tmp_path, monkeypatch, status_slot, meta=download_meta(), stored="old.st"
    )
    assert "civ-SECRET-abcdef" not in json.dumps(status)
