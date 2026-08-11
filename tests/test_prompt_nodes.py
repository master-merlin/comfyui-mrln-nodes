"""Node-level tests: the pack loaded ComfyUI-style with the prompt domain
active, against the real factory content plus a tmp user tier."""

import json

import pytest
import support


@pytest.fixture(autouse=True)
def user_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))
    return tmp_path / "user"


@pytest.fixture(scope="module")
def classes():
    pack = support.load_pack()
    return pack.NODE_CLASS_MAPPINGS


@pytest.fixture()
def template_node(classes):
    return classes["MRLN_PromptTemplate"]()


def test_no_absolute_self_imports():
    """Inside ComfyUI the pack is NOT importable as top-level 'mrln' —
    custom nodes load under the loader's package path, so an absolute
    'from mrln import …' resolves in pytest but explodes at execute time
    (UAT-caught: ModuleNotFoundError in Prompt Enhance). Everything
    package-internal must import relatively."""
    import re
    from pathlib import Path

    package = Path(support.ROOT) / "mrln"
    pattern = re.compile(r"^\s*(?:from mrln[.\s]|import mrln\b)", re.MULTILINE)
    offenders = [
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.fixture()
def section_node(classes):
    return classes["MRLN_PromptSection"]()


def run_template(node, **kw):
    args = {
        "template": "overdrive/full-shot",
        "selection": "",
        "selection_mode": "as configured",
        "seed": 0,
        "format": "template default",
    }
    args.update(kw)
    return node.execute(**args)[:3]  # (prompt, negative, choices); loras tested separately


def test_domain_registered(classes):
    assert "MRLN_PromptTemplate" in classes
    assert "MRLN_PromptSection" in classes
    assert "MRLN_LoraApply" in classes


def test_loras_output_json(classes, template_node, user_tier):
    import json as _json

    (user_tier / "sections" / "lora").mkdir(parents=True, exist_ok=True)
    (user_tier / "sections" / "lora" / "kits.json").write_text(
        _json.dumps(
            {
                "items": [
                    {
                        "name": "bodykit",
                        "text": "HycadeBodykit",
                        "data": {"lora": "kits\\hycade.safetensors", "strength_model": 0.87},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (user_tier / "templates").mkdir(parents=True, exist_ok=True)
    (user_tier / "templates" / "lora-tpl.json").write_text(
        _json.dumps({"slots": [{"id": "kit", "ref": "lora/kits", "default": "bodykit"}]}),
        encoding="utf-8",
    )
    out = template_node.execute(
        template="lora-tpl",
        selection="",
        selection_mode="as configured",
        seed=0,
        format="template default",
    )
    assert "HycadeBodykit" in out[0]
    assert "<lora:" not in out[0]  # signalling rides the loras output, not the prompt
    assert _json.loads(out[3]) == [
        {"lora": "kits\\hycade.safetensors", "strength_model": 0.87, "strength_clip": 0.87}
    ]
    # empty stack renders as an empty JSON list, and LoraApply validates it
    plain = template_node.execute(
        template="overdrive/full-shot",
        selection="",
        selection_mode="as configured",
        seed=0,
        format="template default",
    )
    assert _json.loads(plain[3]) == []
    assert classes["MRLN_LoraApply"].VALIDATE_INPUTS(loras=plain[3]) is True
    assert "not valid JSON" in classes["MRLN_LoraApply"].VALIDATE_INPUTS(loras="{nope")


def test_combos_list_factory_content(classes):
    inputs = classes["MRLN_PromptTemplate"].INPUT_TYPES()
    template_options = inputs["required"]["template"][0]
    assert "overdrive/full-shot" in template_options
    assert "car-shoot" not in template_options  # cleaned up

    section_inputs = classes["MRLN_PromptSection"].INPUT_TYPES()
    section_options = section_inputs["required"]["section"][0]
    assert "location" in section_options  # folder scope
    assert "vehicle/car/color/paint" in section_options  # leaf
    assert "location/automotive" in section_options  # suits-tagged section
    item_options = section_inputs["required"]["item"][0]
    assert item_options[0] == "🎲 random"
    assert "vehicle/car/color/paint/guards-red" in item_options


def test_every_widget_has_tooltip(classes):
    for cls in (classes["MRLN_PromptTemplate"], classes["MRLN_PromptSection"]):
        for group in classes and cls.INPUT_TYPES().values():
            for name, spec in group.items():
                assert len(spec) == 2 and "tooltip" in spec[1], f"{cls.__name__}.{name}"


def test_template_execute_full_shot(template_node):
    prompt, _negative, choices = run_template(template_node)
    assert prompt.startswith("High-resolution photo, advertisement style")
    assert "Car primary color: " in prompt
    assert "(sleek 'Overdrive' license plate:1.2)" in prompt
    assert "template: overdrive/full-shot" in choices


def test_template_deterministic_and_varies(template_node):
    a = run_template(template_node, seed=11)
    b = run_template(template_node, seed=11)
    c = run_template(template_node, seed=12)
    assert a == b
    assert a[0] != c[0]


def test_fixed_slot_pinned_while_random_varies(template_node):
    prompts = [
        run_template(template_node, selection="paint=guards-red", seed=seed) for seed in range(4)
    ]
    for _prompt, _negative, choices in prompts:
        assert "paint: guards-red  [fixed]" in choices
    assert len({p for p, _, _ in prompts}) > 1


def test_trigger_input(template_node):
    prompt, _, _ = run_template(template_node, trigger="SkylineGTR34Vspec")
    assert "SkylineGTR34Vspec style aggressive wide body kit" in prompt


def test_format_override_json(template_node):
    prompt, _, _ = run_template(template_node, format="json", seed=3)
    obj = json.loads(prompt)
    assert "paint" in obj and "prefix" in obj and "variant" in obj


def test_conflict_policy_widget(classes, template_node):
    options = classes["MRLN_PromptTemplate"].INPUT_TYPES()["required"]["conflict_policy"][0]
    assert options == ["negative prevails", "positive prevails"]
    # both policies execute cleanly; they may differ only in the negative
    a = run_template(template_node, seed=5)
    b = run_template(template_node, seed=5, conflict_policy="positive prevails")
    assert a[0] == b[0]


def test_error_surfaces_named(template_node):
    with pytest.raises(Exception, match="unknown slot"):
        run_template(template_node, selection="bogus=1")


def test_section_execute_fixed(section_node):
    text, _negative, choice = section_node.execute(
        section="vehicle/car/color/paint",
        item="vehicle/car/color/paint/guards-red",
        seed=0,
        allow_empty=False,
    )
    assert text == "Guards Red"
    assert choice == "guards-red"


def test_section_execute_random_deterministic(section_node):
    one = section_node.execute(section="location", item="🎲 random", seed=9, allow_empty=False)
    two = section_node.execute(section="location", item="random", seed=9, allow_empty=False)
    assert one == two


def test_section_validate_scope(classes):
    node_cls = classes["MRLN_PromptSection"]
    assert node_cls.VALIDATE_INPUTS(section="location", item="🎲 random") is True
    verdict = node_cls.VALIDATE_INPUTS(
        section="lighting/day", item="vehicle/car/color/paint/guards-red"
    )
    assert "not inside" in verdict


def test_template_validate_selection_mismatch(classes):
    node_cls = classes["MRLN_PromptTemplate"]
    tpl = "overdrive/full-shot"
    assert node_cls.VALIDATE_INPUTS(template=tpl, selection="") is True
    assert node_cls.VALIDATE_INPUTS(template=tpl, selection="paint=guards-red") is True
    # variant slots of any variant are accepted pre-queue (resolve stays strict)
    assert node_cls.VALIDATE_INPUTS(template=tpl, selection="scene=random") is True
    verdict = node_cls.VALIDATE_INPUTS(template=tpl, selection="bogus=1\npaint=guards-red")
    assert "bogus" in verdict and "unknown" in verdict
    assert "not found" in node_cls.VALIDATE_INPUTS(template="nope", selection="x=y")
    assert "name=value" in node_cls.VALIDATE_INPUTS(template=tpl, selection="garbage")


def test_is_changed_reacts_to_user_files(classes, user_tier):
    node_cls = classes["MRLN_PromptTemplate"]
    before = node_cls.IS_CHANGED()
    assert isinstance(before, str)
    (user_tier / "sections").mkdir(parents=True, exist_ok=True)
    (user_tier / "sections" / "extra.json").write_text(
        '{"items": ["something new"]}', encoding="utf-8"
    )
    after = node_cls.IS_CHANGED()
    assert isinstance(after, str) and after != before


def test_user_extension_wins(template_node, user_tier):
    # same-slug user section EXTENDS factory: the new item joins the pool
    paint_dir = user_tier / "sections" / "vehicle" / "car" / "color"
    paint_dir.mkdir(parents=True, exist_ok=True)
    (paint_dir / "paint.json").write_text(
        json.dumps({"items": [{"name": "carbon-grey-matte", "text": "USER OVERRIDE paint"}]}),
        encoding="utf-8",
    )
    prompt, _, choices = run_template(template_node, selection="paint=carbon-grey-matte")
    assert "USER OVERRIDE paint" in prompt
    assert "(user)" in choices
