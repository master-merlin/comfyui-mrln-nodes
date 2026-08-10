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


# sections that predate the text_short convention; everything newer must carry it
_LEGACY_NO_SHORT = (
    "vehicle/car/action",
    "vehicle/car/aero-addon",
    "vehicle/car/caliper",
    "vehicle/car/color/",
    "vehicle/car/design-",
    "vehicle/car/detail",
    "vehicle/car/finish",
    "vehicle/car/graphics",
    "vehicle/car/wheel",
    "location/urban",
    "location/nature",
    "location/studio",
    "location/luxury",
    "location/industrial",
    "location/futuristic",
    "location/automotive",
    "lighting/day",
    "lighting/night",
    "lighting/quality",
    "atmosphere/time-of-day",
    "viewpoint/general",
    "camera/settings",
    "style/grade",
)


def test_new_content_short_variant_coverage(lib):
    """Expansion rule: every post-restructure section keeps >=90% text_short
    coverage so short mode works across the whole library."""
    for slug in lib.section_slugs():
        if any(slug.startswith(prefix) for prefix in _LEGACY_NO_SHORT):
            continue
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
    """Policy: characteristics, never artist or studio names."""
    banned = re.compile(
        r"\b(ghibli|shinkai|wlop|greg rutkowski|artgerm|leibovitz|lindbergh|mucha|banksy)\b", re.I
    )
    for slug in lib.section_slugs():
        if not slug.startswith("style/"):
            continue
        for item in lib.load_section(slug).items:
            assert not banned.search(item.text), f"'{slug}/{item.name}' names an artist/studio"


def test_alias_table_empty_pre_release(lib):
    """Nothing has shipped, so pre-release renames were remapped directly
    and the alias table starts empty. From the first release on, renames
    add entries here instead (mechanism covered by test_prompt_resilience)."""
    data = json.loads((FACTORY_ROOT / "aliases.json").read_text(encoding="utf-8"))
    assert data["sections"] == {} and data["templates"] == {}
