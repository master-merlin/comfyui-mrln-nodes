"""The `gen_info` output of the Prompt Template node (SPEC 6.4, ruling D5).

The pack emits generation metadata as a STRING for a metadata-capable save
node to embed; it does no image IO. So the contract these tests pin is a
string contract, in the A1111 `parameters` dialect:

* what it says is exactly what the render knows — the prompts it emitted, the
  seed it actually drew with, the Civitai identity of the LoRAs it selected,
* what it does NOT say is anything it would have to guess (Steps, Sampler, CFG
  scale, Model live on the sampler/checkpoint nodes) and anything it would
  have to fake (a LoRA with no Civitai AIR is left out, not half-named),
* it round-trips: parsing the string back gives the same prompt strings the
  node put on its own `prompt`/`negative` outputs.

CIVITAI RESOURCE SHAPE — verified 2026-08-12 against Civitai's own uploader
parser, civitai/src/utils/metadata/automatic.metadata.ts:

    type CivitaiResource = { weight?, air?, modelVersionId?, type?,
                             versionName?, modelName? }
    const civitaiResources = /, Civitai resources:\\s*(\\[\\{.*?\\}\\])/;

read from source, not from a screenshot of an image — but never round-tripped
through a live civitai.com upload from this pack. `RESOURCE_FIXTURE` below is
the one place that shape is written down: if a live upload ever proves the
casing wrong, correct that literal and this file fails until the emitter
agrees with it.
"""

import json

import pytest
import support

# The exact bytes we claim Civitai parses. One literal, one place to correct.
RESOURCE_FIXTURE = '[{"type":"lora","weight":1,"modelVersionId":2065365}]'

AIR_ONE = "urn:air:flux1:lora:civitai:1825103@2065365"
AIR_TWO = "urn:air:sdxl:lora:civitai:89032@525084"

COLORS = [
    {"name": "red", "text": "bright red"},
    {"name": "green", "text": "deep green"},
    {"name": "blue", "text": "ocean blue"},
]
LORAS = [
    {
        "name": "f40",
        "text": "FerrariF40, a 1987 Ferrari F40",
        "data": {
            "lora": "flux_f40.safetensors",
            "strength_model": 1.0,
            "strength_clip": 1.0,
            "comment": AIR_ONE,
        },
    },
    {
        "name": "film",
        "text": "35mm film grain",
        "data": {
            "lora": "sdxl_film.safetensors",
            "strength_model": 0.65,
            "strength_clip": 0.8,
            "comment": AIR_TWO,
        },
    },
    {
        # a locally trained LoRA nobody published: we know the file, not who
        # owns it — there is no honest resource entry to emit for it
        "name": "homebrew",
        "text": "MyOwnStyle, hand-trained look",
        "data": {"lora": "homebrew.safetensors", "strength_model": 0.5},
    },
]


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def user_tier(tmp_path, monkeypatch):
    user = tmp_path / "user"
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(user))
    _write(user, "sections/gi/color.json", {"label": "Color", "items": COLORS})
    _write(user, "sections/gi/kit.json", {"label": "Kit", "items": LORAS})
    render = {"format": "string", "joiner": ", "}
    _write(
        user,
        "templates/gi/plain.json",
        {
            "label": "Plain",
            "prefix": "a car",
            "slots": [{"id": "color", "ref": "gi/color", "default": "random"}],
            "render": render,
        },
    )
    _write(
        user,
        "templates/gi/negative.json",
        {
            "label": "With negative",
            "prefix": "a car",
            "slots": [{"id": "color", "ref": "gi/color", "default": "red"}],
            "negative": "blurry, watermark",
            "render": render,
        },
    )
    _write(
        user,
        "templates/gi/lora.json",
        {
            "label": "With loras",
            "prefix": "a car",
            "slots": [
                {"id": "kit", "ref": "gi/kit", "default": "f40"},
                {"id": "grade", "ref": "gi/kit", "default": "film"},
            ],
            "render": render,
        },
    )
    _write(
        user,
        "templates/gi/noair.json",
        {
            "label": "Unpublished lora",
            "prefix": "a car",
            "slots": [{"id": "kit", "ref": "gi/kit", "default": "homebrew"}],
            "render": render,
        },
    )
    return user


@pytest.fixture(scope="module")
def classes():
    return support.load_pack().NODE_CLASS_MAPPINGS


@pytest.fixture()
def node(classes):
    return classes["MRLN_PromptTemplate"]()


def run(node, template="gi/plain", **kw):
    args = {
        "template": template,
        "selection": "",
        "selection_mode": "as configured",
        "seed": 7,
        "format": "template default",
    }
    args.update(kw)
    return node.execute(**args)


GEN_INFO = 5  # output index; the append-only proof below is what guards it


def parse(text):
    """The reader's half of the contract: A1111 `parameters` back to a dict.

    Deliberately naive (split on the marker lines, parse the tail as a
    comma-separated key list) — that is all a consumer does, so if the emitter
    ever needs more than that to be read back, the format has drifted.
    """
    head, _, tail = text.rpartition("\n")
    fields = {}
    resources = None
    marker = ", Civitai resources: "
    if marker in tail:
        tail, _, payload = tail.partition(marker)
        resources = json.loads(payload)
    for pair in tail.split(", "):
        key, _, value = pair.partition(": ")
        fields[key] = value
    positive, sep, negative = head.partition("\nNegative prompt: ")
    return {
        "positive": positive,
        "negative": negative if sep else None,
        "fields": fields,
        "resources": resources,
    }


# ---------------------------------------------------------------------------
# Round-trip: the string must give back the prompts the node emitted
# ---------------------------------------------------------------------------


def test_round_trips_the_prompt_and_negative_it_was_built_from(node):
    prompts, _llm, _loras, negatives, _choices, gen_info = run(node, template="gi/negative")
    parsed = parse(gen_info[0])
    assert parsed["positive"] == prompts[0]
    assert parsed["negative"] == negatives[0]
    assert parsed["negative"] == "blurry, watermark"
    # first line IS the prompt: a consumer that only reads line 1 is correct
    assert gen_info[0].splitlines()[0] == prompts[0] == "a car, bright red"


def test_empty_negative_omits_the_line_rather_than_emitting_an_empty_one(node):
    """`Negative prompt:` with nothing after it is a real negative prompt of
    the empty string to a parser — worse than silence."""
    negatives, gen_info = run(node)[3], run(node)[GEN_INFO]
    assert negatives[0] == ""
    assert "Negative prompt" not in gen_info[0]
    assert gen_info[0] == "a car, bright red\nSeed: 7"
    assert parse(gen_info[0])["negative"] is None


def test_omits_every_field_it_would_have_to_guess(node):
    """The honesty rule: Steps/Sampler/CFG/Model live on the sampler and
    checkpoint nodes. A guessed value here would travel with the shared image
    as if it were true."""
    text = run(node, template="gi/lora")[GEN_INFO][0]
    for key in ("Steps:", "Sampler:", "CFG scale:", "Model:", "Model hash:", "Size:"):
        assert key not in text
    assert set(parse(text)["fields"]) == {"Seed"}


# ---------------------------------------------------------------------------
# Seed: the one actually drawn with, per item
# ---------------------------------------------------------------------------


def test_seed_is_the_seed_actually_used(node):
    assert parse(run(node, seed=4242)[GEN_INFO][0])["fields"]["Seed"] == "4242"


def test_each_batch_item_reports_its_own_seed(node):
    """A batch is N different draws; a gen_info that repeated the master seed
    would make N-1 of the shared images unreproducible."""
    out = run(node, seed=7, batch_count=4)
    gen_info = out[GEN_INFO]
    assert len(gen_info) == 4
    assert [parse(t)["fields"]["Seed"] for t in gen_info] == ["7", "8", "9", "10"]
    # and each one carries ITS item's prompt, not item 0's
    assert [parse(t)["positive"] for t in gen_info] == out[0]


def test_combinatorial_items_report_the_master_seed_they_all_ran_on(node):
    """Combinatorial mode pins the slots instead of re-seeding, so every item
    genuinely IS the master seed — reporting seed+i there would be a lie."""
    gen_info = run(node, seed=7, batch_mode="combinatorial")[GEN_INFO]
    assert len(gen_info) == 3  # three colors
    assert {parse(t)["fields"]["Seed"] for t in gen_info} == {"7"}


# ---------------------------------------------------------------------------
# Civitai resources
# ---------------------------------------------------------------------------


def test_no_loras_means_no_resources_key_at_all(node):
    text = run(node)[GEN_INFO][0]
    assert "Civitai resources" not in text
    assert parse(text)["resources"] is None


def test_lora_bearing_render_emits_the_airs_with_their_strengths(node):
    out = run(node, template="gi/lora")
    text = out[GEN_INFO][0]
    resources = parse(text)["resources"]
    # ids and weights come from the AIR urn + strength_model of each entry
    assert resources == [
        {"type": "lora", "weight": 1, "modelVersionId": 2065365},
        {"type": "lora", "weight": 0.65, "modelVersionId": 525084},
    ]
    # every drawn LoRA on the `loras` wire that has an AIR is accounted for
    entries = json.loads(out[2][0])
    with_air = [e for e in entries if e.get("air")]
    assert len(with_air) == len(resources)
    assert [e["strength_model"] for e in with_air] == [1.0, 0.65]
    # strength_clip differs from strength_model on the second one and must not
    # be smuggled in: A1111's LoRA weight is one number
    assert with_air[1]["strength_clip"] == 0.8
    assert all(set(r) == {"type", "weight", "modelVersionId"} for r in resources)


def test_lora_without_an_air_is_skipped_not_half_emitted(node):
    """We know the file name, not who published it. A resource entry without a
    model version is malformed; one with a guessed version is a false
    attribution against someone else's model."""
    out = run(node, template="gi/noair")
    entries = json.loads(out[2][0])
    assert [e["lora"] for e in entries] == ["homebrew.safetensors"]  # it DID draw
    assert "air" not in entries[0]
    text = out[GEN_INFO][0]
    assert "Civitai resources" not in text  # ...and contributes nothing here
    assert text.endswith("Seed: 7")


def test_resource_payload_matches_the_locked_civitai_fixture(node):
    """The one assertion that pins the exact bytes we claim Civitai parses.
    See this module's docstring: shape read from Civitai's parser source, not
    yet confirmed by a live upload. A correction is this literal + nothing."""
    text = run(node, template="gi/lora", selection="grade=off")[GEN_INFO][0]
    assert text.endswith(f"Seed: 7, Civitai resources: {RESOURCE_FIXTURE}")
    assert json.loads(RESOURCE_FIXTURE) == parse(text)["resources"]


def test_resource_line_shape_is_what_civitais_regex_needs(node):
    """`/, Civitai resources:\\s*(\\[\\{.*?\\}\\])/` — a leading comma, and the
    array on ONE line starting `[{`. Compact separators and a tail line that
    always begins with `Seed:` are what satisfy that, so pin both."""
    text = run(node, template="gi/lora")[GEN_INFO][0]
    tail = text.splitlines()[-1]
    assert tail.startswith("Seed: ")
    assert ", Civitai resources: [{" in tail
    assert "\n" not in tail and ", " not in tail.partition("Civitai resources: ")[2]


def test_integral_weights_serialize_as_ints_like_civitai_written_images(node):
    """`"weight":1`, not `"weight":1.0` — the dialect real Civitai images
    carry. Fractional weights stay fractional."""
    text = run(node, template="gi/lora")[GEN_INFO][0]
    assert '"weight":1,' in text
    assert '"weight":0.65,' in text


# ---------------------------------------------------------------------------
# It is an output like every other one
# ---------------------------------------------------------------------------


def test_gen_info_arrives_as_a_list_of_strings_like_every_other_output(node):
    out = run(node)
    assert len(out) == 6
    assert all(isinstance(values, list) and values for values in out)
    assert all(isinstance(value, str) for values in out for value in values)
    assert isinstance(out[GEN_INFO], list) and isinstance(out[GEN_INFO][0], str)


def test_gen_info_is_deterministic_for_a_seed(node):
    assert run(node, template="gi/lora")[GEN_INFO] == run(node, template="gi/lora")[GEN_INFO]


def test_output_declarations_stay_consistent(classes):
    cls = classes["MRLN_PromptTemplate"]
    assert len(cls.RETURN_TYPES) == len(cls.RETURN_NAMES) == len(cls.OUTPUT_IS_LIST) == 6
    assert cls.OUTPUT_IS_LIST == (True,) * 6
    assert set(cls.RETURN_TYPES) == {"STRING"}
    # outputs are documented, and the tooltip states the omissions explicitly
    assert len(cls.OUTPUT_TOOLTIPS) == 6
    tooltip = cls.OUTPUT_TOOLTIPS[GEN_INFO].lower()
    assert "a1111" in tooltip and "seed" in tooltip and "civitai" in tooltip
    for absent in ("steps", "sampler", "cfg"):
        assert absent in tooltip, f"tooltip must say {absent} is NOT included"


# ---------------------------------------------------------------------------
# Append-only proof (outputs are linked BY INDEX in saved workflows)
# ---------------------------------------------------------------------------

# Snapshot taken at acb5a53, before gen_info existed. Outputs are wired by
# INDEX in a saved workflow, so appending is the only safe edit: extend a list
# at its END, never touch an existing entry.
FROZEN_OUTPUTS = {
    # re-cut once before shipping (see test_protocol_nodes.FROZEN_ORDER): the
    # outputs are grouped by what you wire them to. What this test still owns
    # is that gen_info is LAST and that nothing else appeared or vanished.
    "MRLN_PromptTemplate": ["prompt", "llm", "loras", "negative", "choices"],
    "MRLN_PromptSection": ["text", "negative", "choice"],
    "MRLN_LoraApply": ["model", "clip", "report"],
    "MRLN_PromptEnhance": ["prompt", "report"],
    "MRLN_ShowText": ["text"],
}
# the whole diff of this change, stated as data
APPENDED = {"MRLN_PromptTemplate": ["gen_info"]}


def test_gen_info_is_strictly_appended_and_nothing_else_moved(classes):
    assert set(classes) == set(FROZEN_OUTPUTS), "node id set changed (IDs are frozen)"
    for node_id, frozen in FROZEN_OUTPUTS.items():
        actual = list(classes[node_id].RETURN_NAMES)
        assert actual == frozen + APPENDED.get(node_id, []), node_id
        # the pre-existing outputs are byte-identical, at their old indices
        assert actual[: len(frozen)] == frozen, node_id
    assert list(classes["MRLN_PromptTemplate"].RETURN_NAMES)[-1] == "gen_info"


def test_no_widget_changed_for_this_output(classes):
    """An output was added; `widgets_values` is positional and must not have
    been touched by it."""
    inputs = classes["MRLN_PromptTemplate"].INPUT_TYPES()
    assert list(inputs["required"]) == [
        "template",
        "template_names",
        "trigger",
        "selection",
        "selection_mode",
        "seed",
        "format",
        "text_length",
        "conflict_policy",
    ]
    assert list(inputs["optional"]) == ["variables", "profile", "batch_count", "batch_mode"]
