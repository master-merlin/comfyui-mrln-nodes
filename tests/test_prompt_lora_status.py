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


def test_on_missing_widget_is_optional_and_last(monkeypatch):
    inputs = node_class().INPUT_TYPES()
    assert "on_missing" in inputs["optional"]
    options, spec = inputs["optional"]["on_missing"]
    assert options == ["error", "skip", "download"]
    assert spec["default"] == "error"  # never surprise a user with a download
    # widgets_values is positional: on_missing must be the LAST input
    assert [*inputs["required"], *inputs["optional"]][-1] == "on_missing"


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
