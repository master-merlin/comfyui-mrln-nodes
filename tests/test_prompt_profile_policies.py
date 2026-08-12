"""Phase 3 authoring lint: the render POLICY data shipped in
mrln/data/prompt/profiles.json.

test_prompt_render_policy.py pins the mechanism against a synthetic library.
This file pins the CONTENT — the 29 profiles' block_order + negative_policy and
the ideogram4 JSON scaffold — against the real factory catalog, because the
authoring is where the product value is and where the mistakes are silent:
a rank on a domain that does not exist is dead data, and a block_order that
contradicts the order its own llm.system tells the LLM to write is a profile
arguing with itself.
"""

import json
from itertools import pairwise
from pathlib import Path

import pytest
import support

from mrln.promptlib import (
    NEUTRAL_RANK,
    Library,
    RenderPolicy,
    block_domain,
    compose,
    fill_json_template,
)
from mrln.promptlib.render import NEGATIVE_POLICIES

FACTORY_ROOT = Path(support.ROOT) / "mrln" / "data" / "prompt"

# domain groups, named the way the profiles' own system prompts name them
SUBJECT = ("human", "vehicle", "animal", "creature", "product", "food", "treasure", "boudoir")
SETTING = ("location", "architecture", "nature")
FRAMING = ("composition", "viewpoint")


@pytest.fixture(scope="module")
def lib():
    return Library(FACTORY_ROOT, None)


@pytest.fixture(scope="module")
def profiles():
    return json.loads((FACTORY_ROOT / "profiles.json").read_text(encoding="utf-8"))["profiles"]


def ranks_of(profiles, name):
    policy = RenderPolicy.from_render(profiles[name].get("render") or {}, profile=name)
    return (policy.block_order if policy else None) or {}


# -- every shipped policy parses --------------------------------------------


def test_every_profile_policy_parses(profiles):
    """The whole point of RenderPolicy.from_render raising: a malformed policy
    must never reach a user's render. Nothing in the shipped file may raise."""
    assert len(profiles) >= 29
    for name, prof in profiles.items():
        RenderPolicy.from_render(prof.get("render") or {}, profile=name)  # raises on bad data


def test_every_profile_policy_survives_a_real_compose(lib, profiles):
    """from_render is called inside compose(), so a bad rank would only show up
    at render time — drive the real pipeline once per profile."""
    tpl = lib.load_template("overdrive/full-shot")
    for name in profiles:
        composed = compose(
            lib, tpl, seed=7, mode="as configured", selection={}, variables={}, profile=name
        )
        assert composed.rendered.positive.strip(), name


def test_negative_policy_values_are_known_and_presets_are_filled(profiles):
    for name, prof in profiles.items():
        policy = (prof.get("render") or {}).get("negative_policy")
        assert policy in NEGATIVE_POLICIES, f"{name}: {policy!r}"
        if policy == "preset":
            assert ((prof.get("render") or {}).get("negative_preset") or "").strip(), (
                f"{name}: negative_policy 'preset' with no preset to substitute"
            )


# -- ranked domains must be real --------------------------------------------


def test_every_ranked_domain_exists_as_a_section_domain(lib, profiles):
    """A block domain is the first segment of a section slug. Ranking a domain
    the library does not have is data that can never move a block — this test
    is the one that catches an invented taxonomy."""
    real = {slug.split("/", 1)[0] for slug in lib.section_slugs()}
    for name in profiles:
        ghosts = sorted(set(ranks_of(profiles, name)) - real)
        assert not ghosts, f"profile '{name}' ranks non-existent domain(s): {ghosts}"


def test_ranks_never_collide_with_the_neutral_rank(profiles):
    """NEUTRAL_RANK is where UNLISTED domains sort. Naming a domain and then
    giving it exactly that rank says nothing, so it is always an authoring
    slip: pick a side of 50."""
    for name in profiles:
        for domain, rank in ranks_of(profiles, name).items():
            assert rank != NEUTRAL_RANK, f"{name}/{domain} ranked at the neutral rank"


def test_only_the_audio_profile_ships_without_a_block_order(profiles):
    """Every visual family has a documented reading order. ace-step15 is the
    one honest exception — the library ships no audio section domain, so it
    carries a _comment saying so instead of invented ranks."""
    bare = sorted(name for name in profiles if not ranks_of(profiles, name))
    assert bare == ["ace-step15"]
    assert "no audio section domain" in profiles["ace-step15"]["_comment"]


# -- the policy must agree with the profile's own system prompt --------------

# (profile, [(phrase in llm.system, domains that phrase covers), ...]).
# Both orders are asserted: the phrases must appear in the system prompt in the
# listed order, AND the ranks must be strictly increasing group by group. So a
# rewrite of either side that drifts from the other fails here.
STATED_ORDER = {
    "krea2": [
        ("subject and pose", (*SUBJECT, "pose")),
        ("medium and surface texture", ("style",)),
        ("light behavior and mood", ("lighting", "atmosphere")),
    ],
    "flux": [
        ("subject in the first sentence", SUBJECT),
        ("how the light falls", ("lighting",)),
        ("camera and lens", ("camera",)),
    ],
    "flux2-klein": [
        ("subject and its placement first", SUBJECT),
        ("then action", ("pose",)),
        ("then environment", SETTING),
        ("then light", ("lighting",)),
        ("materials and camera", ("camera",)),
    ],
    "ernie-image": [
        ("subject, composition", SUBJECT),
        ("composition and spatial layout", FRAMING),
        ("lighting, tone", ("lighting",)),
    ],
    "boogu-image": [
        ("covering subject", SUBJECT),
        ("environment", SETTING),
        ("light and materials", ("lighting",)),
    ],
    "qwen-image": [
        ("main subject first", SUBJECT),
        ("then environment", SETTING),
    ],
    "hidream": [
        ("subject and composition first", (*SUBJECT, "composition")),
        ("then mood", ("atmosphere",)),
        ("palette and material quality", ("style",)),
    ],
    "sdxl": [
        ("subject first", SUBJECT),
        ("then setting", SETTING),
        ("lighting", ("lighting",)),
        ("style", ("style",)),
    ],
    "sd15": [
        ("subject and its two or three defining attributes first", SUBJECT),
        ("then setting and light", (*SETTING, "lighting")),
        ("style and quality tags", ("style",)),
    ],
    "pony": [
        ("the subject and", SUBJECT),
        ("scene as booru tags", SETTING),
    ],
    "illustrious": [
        ("character and subject tags", SUBJECT),
        ("then pose", ("pose",)),
        ("clothing", ("wardrobe",)),
        ("scene", SETTING),
        ("lighting", ("lighting",)),
        ("quality tags", ("style",)),
    ],
    "microsoft-lens": [
        ("subject and placement", SUBJECT + FRAMING),
        ("then environment", SETTING),
        ("then light", ("lighting",)),
        ("materials and atmosphere", ("style", "atmosphere")),
    ],
    "zimage": [
        ("ordered subject", SUBJECT),
        ("scene", SETTING),
        ("lighting", ("lighting",)),
        ("style", ("style",)),
    ],
    "chroma": [
        ("front-loading the subject", SUBJECT),
        ("comma tag tail for style anchors", ("style",)),
    ],
    "lumina2": [
        ("explicit spatial relations", FRAMING),
        ("light behavior", ("lighting",)),
        ("content before style", ("style",)),
    ],
    "longcat-image": [
        ("ordered sentences: subject", SUBJECT),
        ("action", ("pose",)),
        ("scene", SETTING),
        ("lighting", ("lighting",)),
        ("style", ("style",)),
    ],
    "nucleus-image": [
        ("subject and placement first", SUBJECT + FRAMING),
        ("then environment", SETTING),
        ("light and materials", ("lighting",)),
    ],
    "dreamlite": [
        ("covering only subject", SUBJECT),
        ("setting and light", (*SETTING, "lighting")),
        ("cut secondary detail", ("style", "camera")),
    ],
    "omnigen2": [
        ("stating subject", SUBJECT),
        ("scene", SETTING),
        ("lighting", ("lighting",)),
        ("style", ("style",)),
    ],
    "ovis-image": [
        ("explicit composition", FRAMING),
        ("and light", ("lighting",)),
    ],
    "prx": [
        ("sentences — subject", SUBJECT),
        ("scene, light", (*SETTING, "lighting")),
    ],
    "ideogram4": [
        ("order is subject", SUBJECT),
        ("then pose or action", ("pose",)),
        ("then secondary elements", ("battle",)),
        ("then setting and background", SETTING),
        ("then lighting and atmosphere", ("lighting", "atmosphere")),
        ("then framing and composition", FRAMING),
        ("then technical enhancers", ("camera",)),
    ],
    "ltx2": [
        ("subject and its continuous action first", (*SUBJECT, "pose")),
        ("then environment", SETTING),
        ("one clean explicit camera move", ("camera",)),
        ("then lighting and style", ("lighting", "style")),
    ],
    "wan21": [
        ("orders subject", SUBJECT),
        ("scene", SETTING),
        ("motion", ("pose",)),
        ("camera language", ("camera",)),
        ("atmosphere", ("atmosphere",)),
        ("style", ("style",)),
    ],
    "wan22": [
        ("subject and motion", (*SUBJECT, "pose")),
        ("one explicit speed-qualified camera move", ("camera",)),
        ("scene", SETTING),
        ("then lighting and composition descriptors", ("lighting", "composition")),
    ],
    "hunyuan-video15": [
        ("wants scene setting", SETTING),
        ("narrate the subject", SUBJECT),
        ("name the shot type", ("camera",)),
        ("close with light and mood", ("lighting", "atmosphere")),
    ],
    "kandinsky5": [
        ("continuous shot: subject", SUBJECT),
        ("its motion", ("pose",)),
        ("one camera move", ("camera",)),
        ("setting, light", (*SETTING, "lighting")),
    ],
    "bernini-r": [
        ("subject appearance", SUBJECT),
        ("materials", ("style",)),
        ("lighting and mood", ("lighting", "atmosphere")),
        ("motion and framing come from the source video", ("pose", *FRAMING, "camera")),
    ],
}


@pytest.mark.parametrize("name", sorted(STATED_ORDER))
def test_block_order_agrees_with_the_profiles_own_system_prompt(profiles, name):
    system = profiles[name]["llm"]["system"].lower()
    ranks = ranks_of(profiles, name)
    steps = STATED_ORDER[name]
    positions = []
    for phrase, domains in steps:
        where = system.find(phrase.lower())
        assert where >= 0, f"{name}: llm.system no longer says '{phrase}' — re-read it"
        positions.append(where)
        present = [d for d in domains if d in ranks]
        assert present, f"{name}: '{phrase}' names {domains}, none of them ranked"
    assert positions == sorted(positions), (
        f"{name}: the system prompt now states these steps in a different order — "
        "the block_order and the prose must be re-reconciled, not silently diverge"
    )
    for (phrase, domains), (next_phrase, next_domains) in pairwise(steps):
        here = max(ranks[d] for d in domains if d in ranks)
        there = min(ranks[d] for d in next_domains if d in ranks)
        assert here < there, (
            f"{name}: '{phrase}' ranks at {here} but '{next_phrase}' ranks at {there} — "
            "the profile tells the LLM one order and reorders blocks into another"
        )


def test_every_profile_with_a_policy_is_covered_by_the_agreement_check(profiles):
    """No profile may quietly gain ranks that nothing checks."""
    ranked = {name for name in profiles if ranks_of(profiles, name)}
    assert ranked == set(STATED_ORDER)


# -- negative_policy: drop only where the family really has no negative ------

# Each entry: the substring of that profile's own documentation that justifies
# suppressing the negative. ideogram4's lives in the profile's _comment because
# Ideogram 4.0 has no negative field anywhere in its API, not just at CFG 1.
DROP_JUSTIFICATION = {
    "krea2": ("llm", "There is no negative prompt"),
    "flux": ("llm", "takes no negative prompt at its distilled guidance"),
    "flux2-klein": ("llm", "No negative prompt exists"),
    "zimage": ("llm", "negative prompts are ignored"),
    "ideogram4": ("_comment", "v4 has no negative"),
}


def test_drop_is_only_used_where_the_family_documents_no_negative(profiles):
    dropped = {
        name
        for name, prof in profiles.items()
        if (prof.get("render") or {}).get("negative_policy") == "drop"
    }
    assert dropped == set(DROP_JUSTIFICATION)
    for name, (where, phrase) in DROP_JUSTIFICATION.items():
        text = profiles[name]["llm"]["system"] if where == "llm" else profiles[name]["_comment"]
        assert phrase in text, f"{name}: nothing left in the profile justifies dropping"


def test_templates_keep_their_negative_even_when_their_profile_drops_it(lib, profiles):
    """The settled rule: a profile is a DEFAULT the node's widget can switch
    away from, so 'drop' suppresses at render time and never licenses an empty
    negative on disk."""
    dropping = {
        name
        for name, prof in profiles.items()
        if (prof.get("render") or {}).get("negative_policy") == "drop"
    }
    checked = 0
    for slug in lib.template_slugs():
        tpl = lib.load_template(slug)
        pinned = tpl.render.profile
        if pinned not in dropping:
            continue
        assert tpl.negative.strip(), f"{slug}: pins '{pinned}' but ships no negative"
        composed = compose(
            lib, tpl, seed=4, mode="as configured", selection={}, variables={}, profile=pinned
        )
        assert composed.rendered.negative == ""
        assert "negative: dropped" in composed.rendered.choices
        checked += 1
    assert checked >= 3  # krea2, zimage and ideogram4 showcases at minimum


# -- the reorder never changes WHAT was drawn --------------------------------

CORPUS = (
    "showcase/ideogram4-type-poster",
    "overdrive/full-shot",
    "portrait/studio",
    "design/travel-poster",
    "vehicle/night-ride",
)


@pytest.mark.parametrize("slug", CORPUS)
def test_every_profile_reorders_without_changing_the_draw(lib, profiles, slug):
    tpl = lib.load_template(slug)

    def drawn(profile):
        composed = compose(
            lib, tpl, seed=13, mode="randomize all", selection={}, variables={}, profile=profile
        )
        return {s.id: (s.item_name, s.seed_used) for s in composed.resolved.slots}, composed

    baseline, _ = drawn("standard")
    for name in profiles:
        picks, composed = drawn(name)
        assert picks == baseline, f"{slug} under '{name}' drew different items"
        # and the domains the policy sorted by are the real ones
        for slot in composed.resolved.slots:
            if slot.item_name is not None and slot.section_slug:
                assert block_domain(slot) == slot.section_slug.split("/", 1)[0]


def test_a_policy_that_reorders_says_so_in_the_choices_report(lib):
    """Not decoration: the report is how a user learns the prompt they see is
    not the order the template stores."""
    tpl = lib.load_template("design/travel-poster")
    moved = [
        name
        for name in ("ideogram4", "sdxl", "hunyuan-video15", "bernini-r", "wan21")
        if "order: optimized"
        in compose(
            lib, tpl, seed=2, mode="as configured", selection={}, variables={}, profile=name
        ).rendered.choices
    ]
    assert len(moved) >= 3, moved


# -- the ideogram4 JSON scaffold ---------------------------------------------

IDEOGRAM_BOUND = {
    "style": "bold editorial poster design, high contrast",
    "light": "hard low sun raking across the hood",
    "lens": "a 35mm at hip height, deep focus",
    "scene": "a rain-black dock apron, sodium lamps behind",
    "subject": "a long-tail hypercar, teardrop glasshouse over venturi tunnels",
}
# Ideogram's CaptionVerifier checks these orders; fill_json_template preserves
# authored order, so the file IS the contract.
STYLE_KEYS_PHOTO = ["aesthetics", "lighting", "photo", "medium"]


def test_ideogram_scaffold_fills_fully_bound_and_fully_unbound(profiles):
    scaffold = profiles["ideogram4"]["json_template"]
    for slot_texts, negative in ((IDEOGRAM_BOUND, "lowres, watermark"), ({}, "")):
        filled = fill_json_template(scaffold, "A hypercar on a dock apron.", negative, slot_texts)
        assert list(filled) == [
            "high_level_description",
            "style_description",
            "compositional_deconstruction",
        ]
        assert list(filled["style_description"]) == STYLE_KEYS_PHOTO
        # exactly one of photo / art_style, always present — a dropped key here
        # is a broken caption, not a tidied payload
        assert ("art_style" in filled["style_description"]) is False
        deco = filled["compositional_deconstruction"]
        assert list(deco) == ["background", "elements"]
        assert len(deco["elements"]) == 1
        assert list(deco["elements"][0]) == ["type", "desc"]
        assert deco["elements"][0]["type"] == "obj"
        for value in (
            filled["high_level_description"],
            *filled["style_description"].values(),
            deco["background"],
            deco["elements"][0]["desc"],
        ):
            assert isinstance(value, str) and value.strip()
    bound = fill_json_template(scaffold, "P", "N", IDEOGRAM_BOUND)
    unbound = fill_json_template(scaffold, "P", "N", {})
    assert bound != unbound  # the bindings really do bind
    assert bound["style_description"]["photo"] == IDEOGRAM_BOUND["lens"]


def test_ideogram_scaffold_carries_no_negative_and_no_request_fields(profiles):
    scaffold = json.dumps(profiles["ideogram4"]["json_template"])
    assert "{negative}" not in scaffold  # v4 has no negative field anywhere
    for absent in ("aspect_ratio", "resolution", "seed", "style_type", "magic_prompt"):
        assert absent not in scaffold, f"'{absent}' is an Ideogram REQUEST field, not a caption key"
    assert "color_palette" not in scaffold  # needs #RRGGBB, which no slot yields yet


def test_ideogram_profile_keeps_the_lint_clauses_and_the_no_rewrite_note(profiles):
    prof = profiles["ideogram4"]
    system = prof["llm"]["system"]
    assert "FIDELITY:" in system and "STYLE LOCK:" in system
    assert "magic-prompt is disabled" in prof["_comment"]


# -- the showcase ------------------------------------------------------------

SHOWCASE = "showcase/ideogram4-type-poster"


def test_showcase_pins_the_profile_and_renders_a_valid_caption(lib):
    tpl = lib.load_template(SHOWCASE)
    assert tpl.render.profile == "ideogram4"  # picking the template targets the model
    assert tpl.negative.strip()
    for seed in (0, 5, 17, 99):
        composed = compose(
            lib,
            tpl,
            seed=seed,
            mode="randomize all",
            selection={},
            variables={},
            profile="ideogram4",
        )
        payload = json.loads(composed.rendered.positive)
        assert list(payload) == [
            "high_level_description",
            "style_description",
            "compositional_deconstruction",
        ]
        style = payload["style_description"]
        assert list(style) == ["aesthetics", "lighting", "medium", "art_style"]
        assert style["medium"] == "graphic_design"
        elements = payload["compositional_deconstruction"]["elements"]
        assert len(elements) == 2  # fixed arity: hero picture + headline
        assert list(elements[0]) == ["type", "bbox", "desc"]
        assert list(elements[1]) == ["type", "bbox", "text", "desc"]
        for element in elements:
            ymin, xmin, ymax, xmax = element["bbox"]
            assert all(isinstance(v, int) and 0 <= v <= 1000 for v in element["bbox"])
            assert ymin <= ymax and xmin <= xmax
        assert elements[1]["text"].isupper()  # the literal characters, nothing else
        assert composed.rendered.negative == ""  # ideogram4 drops it; the file keeps it


def test_showcase_headline_is_a_slot_not_a_variable(lib):
    """The JSON filler receives slot texts and never sees template variables,
    so a '{headline}' variable could never reach the caption's text field."""
    tpl = lib.load_template(SHOWCASE)
    assert not tpl.variables
    headline = next(s for s in tpl.slots if s.id == "headline")
    assert headline.ref == "style/headline"
    composed = compose(
        lib, tpl, seed=5, mode="randomize all", selection={}, variables={}, profile="ideogram4"
    )
    drawn = next(s for s in composed.resolved.slots if s.id == "headline")
    assert drawn.inline  # woven into the suffix sentence, so it leaves the body
    payload = json.loads(composed.rendered.positive)
    text_element = payload["compositional_deconstruction"]["elements"][1]
    assert text_element["text"] == drawn.text
    assert f"'{drawn.text}'" in payload["high_level_description"]  # single quotes, not double


def test_showcase_carries_no_emphasis_syntax(lib):
    """(text:1.2) is Stable-Diffusion sampler syntax; inside a JSON caption it
    is literal punctuation, so no slot in this template may carry emphasis."""
    tpl = lib.load_template(SHOWCASE)
    for slot in list(tpl.slots) + [s for v in tpl.variants for s in v.slots]:
        assert slot.emphasis is None, slot.id
    composed = compose(
        lib, tpl, seed=8, mode="randomize all", selection={}, variables={}, profile="ideogram4"
    )
    assert ":1." not in composed.rendered.positive


def test_headline_section_ships_literal_copy_only(lib):
    section = lib.load_section("style/headline")
    assert section.suits == ()  # a dimension, not a subject domain
    assert len(section.items) >= 10
    for item in section.items:
        assert item.text == item.text.upper()
        assert item.text == item.text_short
        assert '"' not in item.text and "'" not in item.text  # the picture prints these
        assert len(item.text.split()) <= 3  # long strings are where letters go wrong
