"""Missing-LoRA awareness: the library knows which files it needs, the
endpoint scopes that to one template, and LoRA Apply can heal itself
without the Composer (the headless path)."""

import json
import sys
import types

import pytest
import support
from promptlib_fixtures import build_roots

from mrln import promptapi
from mrln.promptlib import Library

AIR = "urn:air:sdxl:lora:civitai:101@202"


@pytest.fixture()
def lib(tmp_path):
    library = Library(*build_roots(tmp_path))
    library.save_user(
        "sections",
        "lora/kits",
        {
            "items": [
                {
                    "name": "bodykit",
                    "text": "HycadeBodykit",
                    "data": {"lora": "kits/hycade.safetensors", "comment": AIR},
                },
                {
                    "name": "noair",
                    "text": "MysteryKit",
                    "data": {"lora": "kits/mystery.safetensors"},
                },
            ]
        },
    )
    library.save_user(
        "templates",
        "kitted",
        {"slots": [{"id": "kit", "ref": "lora/kits", "default": "bodykit"}]},
    )
    return library


def fake_folder_paths(monkeypatch, installed):
    module = types.SimpleNamespace(
        get_filename_list=lambda kind: list(installed),
        get_folder_paths=lambda kind: ["/loras"],
        get_full_path=lambda kind, name: f"/loras/{name}",
    )
    monkeypatch.setitem(sys.modules, "folder_paths", module)
    return module


def fake_comfy(monkeypatch, loaded=None):
    """`import comfy.sd` runs before any missing-file branch, so every node
    test needs the runtime stubbed even when no file is ever loaded."""
    loaded = [] if loaded is None else loaded
    sd = types.SimpleNamespace(
        load_lora_for_models=lambda m, c, state, sm, sc: (f"{m}+lora", f"{c}+lora")
    )
    utils = types.SimpleNamespace(
        load_torch_file=lambda path, safe_load=True: loaded.append(path) or {}
    )
    comfy = types.ModuleType("comfy")
    comfy.sd, comfy.utils = sd, utils
    monkeypatch.setitem(sys.modules, "comfy", comfy)
    monkeypatch.setitem(sys.modules, "comfy.sd", sd)
    monkeypatch.setitem(sys.modules, "comfy.utils", utils)
    return loaded


# -- status scan -------------------------------------------------------------


def test_status_without_comfyui_cannot_judge(lib, monkeypatch):
    monkeypatch.setitem(sys.modules, "folder_paths", None)
    body = promptapi.lora_status(lib)
    assert body["can_download"] is False
    assert body["total"] == 2 and body["missing"] == 0  # nothing to compare against
    assert all(row["present"] for row in body["loras"])


def test_status_flags_missing_and_air(lib, monkeypatch):
    fake_folder_paths(monkeypatch, ["kits/hycade.safetensors"])
    body = promptapi.lora_status(lib)
    assert (body["total"], body["missing"], body["can_download"]) == (2, 1, True)
    rows = {r["item"]: r for r in body["loras"]}
    assert rows["bodykit"]["present"] is True
    assert rows["noair"]["present"] is False
    assert rows["noair"]["air"] == ""  # cannot self-heal, UI must say so
    assert rows["bodykit"]["air"] == AIR


def test_status_matches_slashes_and_case(lib, monkeypatch):
    fake_folder_paths(monkeypatch, ["Kits\\Hycade.SAFETENSORS"])
    rows = {r["item"]: r for r in promptapi.lora_status(lib)["loras"]}
    assert rows["bodykit"]["present"] is True


def test_status_scoped_to_one_template(lib, monkeypatch):
    fake_folder_paths(monkeypatch, [])
    whole = promptapi.lora_status(lib)
    scoped = promptapi.lora_status(lib, "kitted")
    assert whole["total"] == 2
    assert scoped["total"] == 2  # both live in the section the template draws
    other = promptapi.lora_status(lib, "basic")  # draws no LoRA sections
    assert other["total"] == 0 and other["missing"] == 0


def test_status_endpoint(lib, monkeypatch):
    fake_folder_paths(monkeypatch, [])
    status, body = promptapi.handle_lora_status(lib, {"template": "kitted"})
    assert status == 200 and body["template"] == "kitted" and body["missing"] == 2
    status, body = promptapi.handle_lora_status(lib, {})
    assert status == 200 and "template" not in body
    status, _ = promptapi.handle_lora_status(lib, {"template": "ghost"})
    assert status == 404  # unknown template stays a clean 404


def test_status_endpoint_is_registered():
    routes = {(m, p) for m, p, _h, _b in promptapi.ROUTES}
    assert ("get", "/mrln/prompt/lora-status") in routes


def test_hidden_items_are_not_demanded(lib, monkeypatch):
    fake_folder_paths(monkeypatch, [])
    lib.save_user(
        "sections",
        "lora/kits",
        {
            "items": [
                {
                    "name": "bodykit",
                    "text": "HycadeBodykit",
                    "hidden": True,
                    "data": {"lora": "kits/hycade.safetensors", "comment": AIR},
                }
            ]
        },
    )
    files = [r["item"] for r in promptapi.lora_status(lib)["loras"]]
    assert "bodykit" not in files  # a tombstoned item never draws, never needed


# -- the node's headless self-heal -------------------------------------------


def node_class():
    return support.load_pack().NODE_CLASS_MAPPINGS["MRLN_LoraApply"]


def loras_json(air=AIR, name="kits/hycade.safetensors"):
    entry = {"lora": name, "strength_model": 1.0, "strength_clip": 1.0}
    if air:
        entry["air"] = air
    return json.dumps([entry])


def test_optional_widgets_are_append_only(monkeypatch):
    inputs = node_class().INPUT_TYPES()
    options, spec = inputs["optional"]["on_missing"]
    assert options == ["error", "skip", "download"]
    assert spec["default"] == "error"  # never surprise a user with a download
    options, spec = inputs["optional"]["on_mismatch"]
    assert options == ["warn", "skip", "error", "ignore"]
    assert spec["default"] == "warn"  # family detection is best-effort: advise
    # widgets_values is positional — new widgets APPEND, never reorder
    order = [*inputs["required"], *inputs["optional"]]
    assert order[-2:] == ["on_missing", "on_mismatch"]


def test_missing_lora_errors_by_default(monkeypatch):
    fake_folder_paths(monkeypatch, [])
    fake_comfy(monkeypatch)
    node = node_class()()
    with pytest.raises(FileNotFoundError, match="on_missing"):
        node.execute(model="M", clip="C", loras=loras_json())


def test_missing_lora_skip_passes_through(monkeypatch):
    fake_folder_paths(monkeypatch, [])
    fake_comfy(monkeypatch)
    node = node_class()()
    model, clip = node.execute(model="M", clip="C", loras=loras_json(), on_missing="skip")
    assert (model, clip) == ("M", "C")  # untouched, run continues


def test_missing_lora_download_fetches_then_loads(monkeypatch):
    """The headless gap: no Composer involved, the node heals itself."""
    installed = []
    fake_folder_paths(monkeypatch, installed)
    loaded = fake_comfy(monkeypatch)

    cls = node_class()
    api = sys.modules[sys.modules[cls.__module__].__package__.rsplit(".", 1)[0] + ".promptapi"]
    calls = []

    def fake_download(library, air, **kw):
        calls.append((air, kw))
        installed.append("kits/hycade.safetensors")  # the file now exists
        return "kits/hycade.safetensors"

    monkeypatch.setattr(api, "download_lora_by_air", fake_download)
    model, clip = cls().execute(model="M", clip="C", loras=loras_json(), on_missing="download")
    assert calls and calls[0][0] == AIR
    assert (model, clip) == ("M+lora", "C+lora")  # actually applied after the fetch
    assert loaded == ["/loras/kits/hycade.safetensors"]


def test_download_without_air_still_errors(monkeypatch):
    fake_folder_paths(monkeypatch, [])
    fake_comfy(monkeypatch)
    node = node_class()()
    with pytest.raises(FileNotFoundError, match="Composer"):
        node.execute(model="M", clip="C", loras=loras_json(air=""), on_missing="download")


def test_download_by_air_rejects_a_bogus_urn(lib):
    with pytest.raises(RuntimeError, match="not a Civitai AIR"):
        promptapi.download_lora_by_air(lib, "not-an-air")


# -- nested child slots must round-trip through the editor -------------------


def test_section_payload_carries_child_slots(lib):
    """A user item REPLACES a factory one by name, and the editor writes rows
    back whole — so an item's child slots have to reach the client or the
    first edit silently destroys every nested draw (human/profile ships 17)."""
    lib.save_user(
        "sections",
        "crewed",
        {
            "items": [
                {
                    "name": "pair",
                    "text": "a pair: {left} and {right}",
                    "slots": [
                        {"id": "left", "ref": "color", "default": "red"},
                        {"id": "right", "ref": "color", "tags_any": ["warm"]},
                    ],
                }
            ]
        },
    )
    status, body = promptapi.handle_section(lib, {"slug": "crewed"})
    assert status == 200
    slots = body["items"][0]["slots"]
    assert [s["id"] for s in slots] == ["left", "right"]
    assert slots[0]["ref"] == "color" and slots[0]["default"] == "red"
    assert slots[1]["tags_any"] == ["warm"]
    # and the shape is exactly what save-section accepts back
    status, _ = promptapi.handle_save_section(
        lib,
        {"slug": "crewed", "data": {"items": [{**body["items"][0], "origin": None}]}},
    )
    assert status == 200
    assert len(lib.load_section("crewed").items[0].slots) == 2


def test_items_without_children_report_an_empty_list(lib):
    _status, body = promptapi.handle_section(lib, {"slug": "lora/kits"})
    assert all(item["slots"] == [] for item in body["items"])


# -- base-model compatibility ------------------------------------------------


def fake_model(class_name):
    """A stand-in for a loaded MODEL: ComfyUI wraps the architecture object
    on .model, which is what the family sniffer reads."""
    inner = type(class_name, (), {})()
    return types.SimpleNamespace(model=inner)


@pytest.mark.parametrize(
    "class_name,family",
    [
        ("Flux", "flux1"),
        ("SDXL", "sdxl"),
        ("SDXLRefiner", "sdxl"),
        ("QwenImage", "qwen"),
        ("BaseModel", ""),  # unknown architecture disables the check
    ],
)
def test_model_family_detection(class_name, family):
    from mrln.nodes.prompt import model_family

    assert model_family(fake_model(class_name)) == family


def test_model_family_never_raises():
    from mrln.nodes.prompt import model_family

    assert model_family(None) == ""
    assert model_family(object()) == ""


def test_pony_and_illustrious_count_as_sdxl():
    from mrln.nodes.prompt import _canonical_family

    assert _canonical_family("pony") == "sdxl"
    assert _canonical_family("illustrious") == "sdxl"
    assert _canonical_family("FLUX1") == "flux1"


def mismatched(name="kits/hycade.safetensors"):
    return json.dumps([{"lora": name, "base": "flux1"}])


def test_mismatch_warns_but_still_applies(monkeypatch, caplog):
    fake_folder_paths(monkeypatch, ["kits/hycade.safetensors"])
    fake_comfy(monkeypatch)
    _model, clip = node_class()().execute(model=fake_model("SDXL"), clip="C", loras=mismatched())
    assert clip == "C+lora"  # warn = applied anyway, the user decides
    assert any("trained for flux1" in r.message for r in caplog.records)


def test_mismatch_skip_leaves_the_lora_out(monkeypatch):
    fake_folder_paths(monkeypatch, ["kits/hycade.safetensors"])
    loaded = fake_comfy(monkeypatch)
    sentinel = fake_model("SDXL")
    model, clip = node_class()().execute(
        model=sentinel, clip="C", loras=mismatched(), on_mismatch="skip"
    )
    assert (model, clip) == (sentinel, "C") and loaded == []


def test_mismatch_error_names_both_families(monkeypatch):
    fake_folder_paths(monkeypatch, ["kits/hycade.safetensors"])
    fake_comfy(monkeypatch)
    with pytest.raises(ValueError, match=r"trained for flux1.*model is sdxl"):
        node_class()().execute(
            model=fake_model("SDXL"), clip="C", loras=mismatched(), on_mismatch="error"
        )


def test_matching_family_is_silent(monkeypatch, caplog):
    fake_folder_paths(monkeypatch, ["kits/hycade.safetensors"])
    fake_comfy(monkeypatch)
    node_class()().execute(
        model=fake_model("Flux"), clip="C", loras=mismatched(), on_mismatch="error"
    )
    assert not any("trained for" in r.message for r in caplog.records)


def test_undeclared_lora_is_never_a_mismatch(monkeypatch):
    """Most user LoRAs declare no family — they must not spam warnings."""
    fake_folder_paths(monkeypatch, ["kits/hycade.safetensors"])
    fake_comfy(monkeypatch)
    node_class()().execute(
        model=fake_model("SDXL"), clip="C", loras=loras_json(air=""), on_mismatch="error"
    )


def test_ignore_disables_the_check(monkeypatch):
    fake_folder_paths(monkeypatch, ["kits/hycade.safetensors"])
    fake_comfy(monkeypatch)
    node_class()().execute(
        model=fake_model("SDXL"), clip="C", loras=mismatched(), on_mismatch="ignore"
    )
