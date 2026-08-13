"""Content lint for the shipped factory library: everything parses, every
template reference resolves, slugs follow the naming rules, orthogonality
holds (time-of-day words in location items require a day/night declaration),
and the classifier/conflict machinery works against the real content."""

import json
import re
from pathlib import Path

import pytest
import support

from mrln.promptlib import (
    FORMATS,
    Library,
    render,
    resolve_section,
    resolve_template,
)
from mrln.promptlib.resolve import _parse_token
from mrln.promptlib.schema import SLUG_SEGMENT_RE

FACTORY_ROOT = Path(support.ROOT) / "mrln" / "data" / "prompt"
TIME_OF_DAY_WORDS = re.compile(
    r"\b(night|daylight|sunset|sundown|sunrise|dawn|dusk|noon|midday|morning|evening)\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def lib():
    return Library(FACTORY_ROOT, None)


def rt(lib, seed=0, selection=None, variables=None, mode="as configured"):
    tpl = lib.load_template("overdrive/full-shot")
    return tpl, resolve_template(
        lib, tpl, seed=seed, mode=mode, selection=selection or {}, variables=variables or {}
    )


def test_factory_root_exists():
    assert FACTORY_ROOT.is_dir()


def test_no_top_level_leaf_sections():
    """Taxonomy rule: every dimension is a folder — a top-level leaf would
    block nesting under its name forever."""
    for path in (FACTORY_ROOT / "sections").glob("*.json"):
        raise AssertionError(f"top-level leaf section '{path.name}' — move it into a folder")


def test_all_sections_parse_and_slugs_valid(lib):
    slugs = lib.section_slugs()
    assert len(slugs) >= 20
    for slug in slugs:
        for segment in slug.split("/"):
            assert SLUG_SEGMENT_RE.match(segment), f"bad slug segment in '{slug}'"
        section = lib.load_section(slug)
        for item in section.items:
            assert SLUG_SEGMENT_RE.match(item.name), f"bad item name '{item.name}' in '{slug}'"


def test_all_template_refs_resolve(lib):
    for tpl_slug in lib.template_slugs():
        tpl = lib.load_template(tpl_slug)
        all_slots = list(tpl.slots) + [s for v in tpl.variants for s in v.slots]
        for slot in all_slots:
            pool = lib.scope_items(slot.ref)  # raises if the ref is dangling
            kind, value = _parse_token(slot.default, f"{slot.id}={slot.default}")
            if kind == "fixed":
                names = [q for q, _, _ in pool]
                assert value in names, (
                    f"{tpl_slug}: slot '{slot.id}' default '{value}' not in ref '{slot.ref}'"
                )


def test_every_template_renders_all_formats(lib):
    for tpl_slug in lib.template_slugs():
        tpl = lib.load_template(tpl_slug)
        resolved = resolve_template(
            lib, tpl, seed=0, mode="as configured", selection={}, variables={}
        )
        for fmt in FORMATS:
            out = render(resolved, fmt, tpl.render)
            assert out.positive.strip(), f"{tpl_slug}/{fmt} rendered empty"


def test_location_items_are_orthogonal_or_declared(lib):
    """Location models PLACE; an item whose text bakes in time-of-day must
    declare it via requires (feeds the ⚠ report and the It3 validator)."""
    for slug in lib.section_slugs():
        if not slug.startswith("location/"):
            continue
        for item in lib.load_section(slug).items:
            match = TIME_OF_DAY_WORDS.search(item.text)
            if match and not ({"day", "night"} & set(item.requires)):
                raise AssertionError(
                    f"'{slug}/{item.name}' bakes in '{match.group()}' without "
                    "declaring requires: ['day'|'night']"
                )


# -- Referent anchoring (a section name is not a prompt) ----------------------

NAVAL_WORDS = re.compile(
    r"\b(dreadnought|frigate|corvette|battleship|cruiser|destroyer|galleon|schooner"
    r"|trawler|freighter|liner|tanker|skiff|barge|flotilla|armada|convoy|fleet"
    r"|flagship|carrier|hull|bow|stern|amidships|keel|gunwale|prow|quarterdeck"
    r"|forecastle|at anchor|anchorage|moored|drydock)s?\b",
    re.IGNORECASE,
)
SPACEFLIGHT_ANCHOR = re.compile(
    r"\b(spacecraft|spaceship|starship|starfighter|starfaring|starfarer|starliner"
    r"|star[- ](?:dreadnought|freighter|liner|carrier|cruiser|destroyer|frigate)"
    r"|interstellar|sublight|faster-than-light|ftl|hyperspace|lightspeed|warp"
    r"|void|vacuum|orbit|orbital|de-?orbit|re-?entry|atmospheric entry|planetfall"
    r"|thruster|fusion[- ]drive|ion[- ]drive|plasma[- ]drive|drive bell|drive plume"
    r"|engine bell|main drive|solar sail|zero-?g|null-?gravity|repulsor|antigrav)s?\b",
    re.IGNORECASE,
)


def referent_defect(text, text_short):
    """The rule, as a function: text DOMINATED by seagoing-vessel vocabulary
    (>=2 distinct naval terms) must also name itself as spaceflight hardware,
    and text_short must carry that anchor too. Returns a reason or None."""
    naval = {m.group(0).lower() for m in NAVAL_WORDS.finditer(text)}
    naval_short = {m.group(0).lower() for m in NAVAL_WORDS.finditer(text_short)}
    if len(naval) >= 2:
        if not SPACEFLIGHT_ANCHOR.search(text):
            return (
                f"naval vocabulary {sorted(naval)} with no spaceflight anchor — name the "
                "OBJECT (starship / interstellar / fusion-drive / orbital), not the environment"
            )
        if not SPACEFLIGHT_ANCHOR.search(text_short):
            return (
                f"text_short {text_short!r} drops the spaceflight anchor — short is what "
                "tag-style models actually receive"
            )
    elif len(naval_short) >= 2 and not SPACEFLIGHT_ANCHOR.search(text_short):
        return f"text_short {text_short!r} is naval {sorted(naval_short)} with no anchor"
    return None


def test_scifi_vehicles_anchor_their_referent(lib):
    """A SECTION NAME IS NOT A PROMPT. Slug ('vehicle/scifi-fleet') and label
    ('The Fleet') never reach the rendered text, so an item written purely in
    wet-navy vocabulary describes a BOAT to anything reading only that line —
    a base model rides the surrounding template's gestalt, but an LLM asked to
    rewrite it into prose resolves the dominant referent and lands on an
    aircraft carrier with thrusters bolted on.

    The anchor names the OBJECT, never the environment: these items also get
    drawn mid-atmospheric-entry, where 'in vacuum' would contradict the scene.

    Deliberately narrow. Scoped to vehicle/ sci-fi sections, so incidental sea
    words elsewhere stay legal — 'cloud deck' in a location, an anatomical
    'keel' on a creature, barnacled wreckage on an actual beach — and
    vehicle/ship/* keeps its naval words because those really are boats. The
    threshold is 2 so a single flavour word ('speeder skiff', 'drone-carrier
    gunship') never trips it. A one-word item can still be bad prose; this
    test is the floor, not the whole standard."""
    for slug in lib.section_slugs():
        if not slug.startswith("vehicle/"):
            continue
        section = lib.load_section(slug)
        if "scifi" not in section.tags and "scifi" not in slug:
            continue
        for item in section.items:
            reason = referent_defect(item.text, item.text_short or "")
            assert reason is None, f"'{slug}/{item.name}': {reason}"


def test_referent_rule_catches_a_new_defect():
    """The rule must catch newly authored content, not just today's ten items:
    the pre-fix lines are the regression fixtures."""
    assert referent_defect(
        "a wedge of kilometer-class dreadnoughts in staggered echelon, gun batteries the "
        "size of city blocks along each flank, escort frigates holding tight to the hulls",
        "dreadnought wedge formation",
    )
    assert referent_defect(
        "a fleet carrier at the center of its screen, launch bays glowing along the "
        "ventral spine, fighter wings streaming out of the catapult decks",
        "carrier with fighter screen",
    )
    # anchored long text, un-anchored short: SDXL/Pony still get the boat
    assert referent_defect(
        "a battle group limping home, scorched hulls open to vacuum, one cruiser under tow",
        "scarred returning battle group",
    )
    # ...and stays quiet on legitimate incidental use
    assert referent_defect("a low desert speeder skiff kicking up twin dust ribbons", "") is None
    assert (
        referent_defect(
            "a drone-carrier gunship on station, launch cells cycling along its flanks",
            "drone-carrier gunship",
        )
        is None
    )
    assert referent_defect("a starship carrier at the head of its escort screen", "") is None


def test_constraint_demo_present(lib):
    """The neon/night dependency showcase must survive content edits."""
    urban = lib.load_section("location/urban")
    neon = next(i for i in urban.items if i.name == "neon-highway")
    assert "night" in neon.requires
    assert neon.negative == "daylight"


# -- OverDrive / classifier machinery ----------------------------------------


def test_full_shot_template_ships(lib):
    assert "overdrive/full-shot" in lib.template_slugs()
    assert lib.load_template("overdrive/full-shot").type == ("object", "vehicle", "car")


def test_car_sections_declare_suits(lib):
    for slug in lib.section_slugs():
        if slug.startswith("vehicle/car/") or slug == "location/automotive":
            assert lib.load_section(slug).suits == ("object", "vehicle", "car"), slug
        if slug.startswith(("lighting/", "camera/", "style/", "viewpoint/")):
            assert lib.load_section(slug).suits == (), f"{slug} should stay universal"


def test_group_weights_uniform(lib):
    """Weights preserve the original nested-brace draw in the car pools."""
    for slug in ("vehicle/car/color/paint", "vehicle/car/design-base"):
        section = lib.load_section(slug)
        totals = {}
        for item in section.items:
            assert item.tags, f"{slug}/{item.name} lost its group tag"
            totals[item.tags[0]] = totals.get(item.tags[0], 0.0) + item.weight
        values = list(totals.values())
        assert max(values) - min(values) < 0.01, (slug, totals)


def test_full_shot_suffix_variables(lib):
    tpl, resolved = rt(lib, seed=1)
    out = render(resolved, tpl.render.format, tpl.render)
    assert "(HycadeBodykit style aggressive wide body kit:1.1)" in out.positive
    assert "(sleek 'Overdrive' license plate:1.2)" in out.positive
    tpl, resolved = rt(lib, seed=1, variables={"trigger": "MyKit", "plate": "MRLN"})
    out = render(resolved, tpl.render.format, tpl.render)
    assert "MyKit style" in out.positive and "'MRLN' license plate" in out.positive


def test_variant_tag_coupling(lib):
    """Day variant never draws night-tagged scenes; night variant only."""
    seen = set()
    for seed in range(24):
        _, resolved = rt(lib, seed=seed)
        seen.add(resolved.variant)
        scene = next(s for s in resolved.slots if s.id == "scene")
        light = next(s for s in resolved.slots if s.id == "light")
        if resolved.variant == "night":
            assert "night" in scene.tags, (seed, scene.item_name)
            assert light.section_slug == "lighting/night"
        else:
            assert "night" not in scene.tags, (seed, scene.item_name)
            assert light.section_slug == "lighting/day"
    assert seen == {"day", "night"}


def test_fixed_pick_bypasses_tag_filters(lib):
    """Tagging never restricts an explicit choice: a day scene resolves in
    the night variant when named directly."""
    _, resolved = rt(lib, selection={"variant": "night", "scene": "nature/stelvio-pass"})
    scene = next(s for s in resolved.slots if s.id == "scene")
    assert scene.item_name == "nature/stelvio-pass"


def test_suits_exclude_random_draws_but_not_fixed(lib, tmp_path):
    user = tmp_path / "user"
    (user / "sections" / "location").mkdir(parents=True)
    (user / "sections" / "location" / "boudoir.json").write_text(
        json.dumps(
            {
                "suits": ["human", "boudoir"],
                "items": [{"name": "silk-bedroom", "text": "a silk-draped boudoir bedroom"}],
            }
        ),
        encoding="utf-8",
    )
    two_tier = Library(FACTORY_ROOT, user)
    for seed in range(30):  # typed car template never draws the human-suited section
        _tpl, resolved = rt(two_tier, seed=seed)
        scene = next(s for s in resolved.slots if s.id == "scene")
        assert scene.section_slug != "location/boudoir", seed
    _, resolved = rt(two_tier, selection={"variant": "day", "scene": "boudoir/silk-bedroom"})
    scene = next(s for s in resolved.slots if s.id == "scene")
    assert scene.item_name == "boudoir/silk-bedroom"  # explicit pick still works


def test_conflict_policy_and_requires_warning(lib):
    tpl, resolved = rt(
        lib,
        selection={
            "variant": "day",
            "scene": "urban/neon-highway",
            "light": "volumetric-god-rays",
        },
    )
    kept = render(resolved, "string", tpl.render)  # default: negative prevails
    assert "daylight" in kept.negative
    assert "conflict: 'daylight'" in kept.choices and "kept in negative" in kept.choices
    assert "⚠ scene: requires 'night'" in kept.choices  # day lighting drawn, no night tag
    dropped = render(resolved, "string", tpl.render, conflict_policy="positive prevails")
    assert "daylight" not in dropped.negative
    assert "dropped from negative" in dropped.choices


def test_graphics_wildcards_expand(lib):
    hit = False
    for seed in range(20):
        resolved = resolve_section(lib, "vehicle/car/graphics", "random", seed=seed)
        assert "{" not in resolved.text and "|" not in resolved.text
        hit = True
    assert hit


def test_factory_aliases_valid(lib):
    """Released slugs never just die: every alias target must resolve, and
    no alias source may shadow (collide with) a live slug."""
    data = json.loads((FACTORY_ROOT / "aliases.json").read_text(encoding="utf-8"))
    slugs = set(lib.section_slugs())
    folders = set(lib.section_folders())
    for source, target in data.get("sections", {}).items():
        assert source not in slugs | folders, f"alias source '{source}' shadows a live slug"
        assert lib.scope_items(source), f"alias '{source}' -> '{target}' does not resolve"
    for source in data.get("templates", {}):
        assert source not in set(lib.template_slugs()), f"'{source}' shadows a live template"
        lib.load_template(source)  # raises if the chain is dead


def test_new_content_short_variant_coverage(lib):
    """Expansion rule: EVERY section keeps >=90% text_short coverage so short
    mode works across the whole library. The pre-convention sections were
    backfilled in the 2026-08-12 content round — there is no exemption list
    any more, and new content must not reintroduce one."""
    for slug in lib.section_slugs():
        items = lib.load_section(slug).items
        covered = sum(1 for item in items if item.text_short)
        assert covered >= 0.9 * len(items), (
            f"'{slug}': only {covered}/{len(items)} items carry text_short"
        )


def test_human_pools_adult_only(lib):
    """Factory policy: adult age brackets only, everywhere in human/."""
    banned = re.compile(
        r"\b(teen|teenage|child|children|kid|minor|underage|young girl|young boy)\b", re.I
    )
    for slug in lib.section_slugs():
        if not slug.startswith(("human/", "boudoir/", "pose/", "wardrobe/")):
            continue
        for item in lib.load_section(slug).items:
            assert not banned.search(item.text), f"'{slug}/{item.name}' fails the adult-only lint"
    ages = lib.load_section("human/age")
    for item in ages.items:
        assert re.search(r"(twenties|thirties|forties)", item.text), item.name


def test_domain_sections_declare_suits(lib):
    """Every subject-domain section carries its class suits; dimensions stay universal."""
    expectations = {
        "human/": "human",
        "wardrobe/": "human",
        "pose/": "human",
        "animal/": "animal",
        "nature/": "nature",
        "architecture/": "architecture",
        "food/": "food",
        "vehicle/": "vehicle",
    }
    for slug in lib.section_slugs():
        for prefix, required in expectations.items():
            if slug.startswith(prefix):
                assert required in lib.load_section(slug).suits, (
                    f"'{slug}' must declare suits including '{required}'"
                )
    for slug in ("atmosphere/weather", "composition/framing", "camera/film", "style/genre"):
        assert lib.load_section(slug).suits == (), f"{slug} should stay universal"


def test_no_artist_names_in_style_sections(lib):
    """Policy: characteristics, never artist or studio names. Applies to any
    section that reaches the prompt as a STYLE statement — including the
    LoRA-lab showcase, where a community model's trained trigger is not a
    licence to ship a studio name in factory content (user tiers may)."""
    banned = re.compile(
        r"\b(ghibli|shinkai|wlop|greg rutkowski|artgerm|leibovitz|lindbergh|mucha|banksy)\b", re.I
    )
    for slug in lib.section_slugs():
        if not (slug.startswith("style/") or slug.startswith("loralab/")):
            continue
        for item in lib.load_section(slug).items:
            for text in (item.text, item.text_short or ""):
                assert not banned.search(text), f"'{slug}/{item.name}' names an artist/studio"


def test_template_conventions(lib):
    """House rules for factory templates: a real description everywhere;
    labeled-format templates must label every slot (labels are user-facing
    prose lead-ins there) and use a {label}/{text} line pattern."""
    for tpl_slug in lib.template_slugs():
        tpl = lib.load_template(tpl_slug)
        assert len(tpl.description) >= 20, f"{tpl_slug}: description missing or too thin"
        assert tpl.negative, f"{tpl_slug}: every factory template ships a safety negative"
        if tpl.render.format == "string_labeled":
            assert "{label}" in tpl.render.labeled_line and "{text}" in tpl.render.labeled_line
            for slot in list(tpl.slots) + [s for v in tpl.variants for s in v.slots]:
                assert slot.label, f"{tpl_slug}: slot '{slot.id}' needs a label (labeled format)"


def test_human_templates_carry_safety_negatives(lib):
    """Any template that can draw human content carries the adult-safety
    negative terms by default."""
    for tpl_slug in lib.template_slugs():
        tpl = lib.load_template(tpl_slug)
        refs = [s.ref for s in tpl.slots] + [s.ref for v in tpl.variants for s in v.slots]
        if any(ref.startswith(("human", "boudoir", "pose", "wardrobe")) for ref in refs):
            assert "child" in tpl.negative and "underage" in tpl.negative, (
                f"{tpl_slug}: human-drawing template must carry adult-safety negatives"
            )


def test_every_template_family_has_three_choices(lib):
    """The rule the UI presents: every template FOLDER (the family the
    composer and node group by) offers at least 3 templates."""
    groups = {}
    for slug in lib.template_slugs():
        groups.setdefault(slug.split("/")[0], []).append(slug)
    for family, members in sorted(groups.items()):
        assert len(members) >= 3, f"template family '{family}' has only {len(members)}: {members}"


def test_alias_table_empty_pre_release(lib):
    """Nothing has shipped, so pre-release renames were remapped directly
    and the alias table starts empty. From the first release on, renames
    add entries here instead (mechanism covered by test_prompt_resilience)."""
    data = json.loads((FACTORY_ROOT / "aliases.json").read_text(encoding="utf-8"))
    assert data["sections"] == {} and data["templates"] == {}


# -- the era axis -------------------------------------------------------------
# Coherence is the whole product here: a period portrait is only worth having
# if the clothes, the hair, the room and the film stock land in the SAME year.
# That coupling is a tag filter, so these guard the tags rather than the prose.

ERA_TAGS = {"1920s", "wwii", "1950s", "1970s", "1980s", "1990s", "post-apocalypse"}


def test_every_era_item_carries_exactly_one_era_tag(lib):
    """An untagged item would leak into every period; a two-era item would make
    a 1950s draw arrive in 1980s clothes."""
    for slug in (s for s in lib.section_slugs() if s.startswith("era/")):
        for item in lib.load_section(slug).items:
            eras = ERA_TAGS.intersection(item.tags)
            assert len(eras) == 1, (
                f"'{slug}/{item.name}' carries {sorted(eras) or 'no'} era tag(s) — exactly "
                "one is required, or it cannot be coupled to a period"
            )


def test_every_era_has_a_draw_in_every_era_dimension(lib):
    """A period with no wardrobe (or no hair, place, medium) renders a variant
    with a hole in it. Adding an era means adding items to ALL of them."""
    dimensions = sorted(s for s in lib.section_slugs() if s.startswith("era/"))
    assert dimensions, "the era axis vanished"
    for slug in dimensions:
        items = lib.load_section(slug).items
        present = {tag for item in items for tag in ERA_TAGS & set(item.tags)}
        missing = ERA_TAGS - present
        assert not missing, f"'{slug}' has nothing for {sorted(missing)}"


def test_the_period_template_couples_every_era_slot_to_one_period(lib):
    """The bug this prevents: a variant whose wardrobe is filtered to 1950s but
    whose hair is not, which reads as a costume error rather than a draw."""
    tpl = lib.load_template("portrait/period-portrait")
    names = {v.name for v in tpl.variants}
    assert names == ERA_TAGS, f"variants {sorted(names)} != eras {sorted(ERA_TAGS)}"
    for variant in tpl.variants:
        era_slots = [s for s in variant.slots if s.ref.startswith("era/")]
        assert era_slots, f"variant '{variant.name}' draws no era content"
        for slot in era_slots:
            assert list(slot.tags_any) == [variant.name], (
                f"variant '{variant.name}' slot '{slot.id}' filters "
                f"{list(slot.tags_any)} — every era slot must name its own period"
            )


def test_the_period_template_does_not_draw_a_modern_look(lib):
    """human/profile is a complete CONTEMPORARY look — its own hair, makeup and
    clothing — so a period template must not use it (it produced a WWII
    portrait in beachy mermaid waves before this was caught)."""
    tpl = lib.load_template("portrait/period-portrait")
    refs = [s.ref for s in tpl.slots] + [s.ref for v in tpl.variants for s in v.slots]
    assert "human/profile" not in refs, (
        "period-portrait draws human/profile, which supplies modern hair and clothing "
        "that fight the era slots"
    )


def test_no_template_restates_an_axis_its_subject_already_draws(lib):
    """A template must not add a slot for something its own subject already
    draws one level down.

    human/profile's items nest their own wardrobe, hair and makeup. A
    template that ALSO declares an 'outfit' slot puts two wardrobes in one
    prompt and the model picks one — which is exactly how boudoir/pin-up's
    first thumbnail came out in a Grecian evening gown instead of lingerie.
    The tile is what exposed it; this is the check that would have.
    """
    # Argued exception. human/profile's 'female-lingerie' weaves
    # '{gaze} paired with {gesture}' into its text, so the gesture cannot be
    # lifted out without rewriting an item that boudoir/session already
    # renders well. One item of eight, and the two gestures compose rather
    # than contradict — kept deliberately, not overlooked.
    allowed = {("boudoir/vanity-portrait", "ritual", "human/gesture")}

    def nested_refs(ref):
        try:
            pool = lib.scope_items(ref)
        except Exception:  # a folder ref that resolves to nothing here
            return set()
        return {slot.ref for _q, _sec, item in pool for slot in getattr(item, "slots", ()) or ()}

    collisions = []
    for slug in lib.template_slugs():
        tpl = lib.load_template(slug)
        slots = list(tpl.slots) + [s for v in tpl.variants for s in v.slots]
        by_ref = {s.ref: s.id for s in slots}
        for slot in slots:
            for child in nested_refs(slot.ref):
                owner = by_ref.get(child)
                if owner and owner != slot.id and (slug, owner, child) not in allowed:
                    collisions.append(
                        f"{slug}: slot '{owner}' draws {child}, which '{slot.id}' "
                        f"({slot.ref}) already draws for itself"
                    )
    assert not collisions, "\n".join(collisions)
