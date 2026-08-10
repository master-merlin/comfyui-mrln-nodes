"""Content lint for the shipped factory library: everything parses, every
template reference resolves, slugs follow the naming rules, and the
orthogonality principle holds (location items carry no time-of-day words)."""

import re
from pathlib import Path

import pytest
import support

from mrln.promptlib import FORMATS, Library, render, resolve_template
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


def test_factory_root_exists():
    assert FACTORY_ROOT.is_dir()


def test_all_sections_parse_and_slugs_valid(lib):
    slugs = lib.section_slugs()
    assert len(slugs) >= 14
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


def test_location_items_are_orthogonal(lib):
    """Locations model PLACE only; time-of-day belongs to 'atmosphere'."""
    for slug in lib.section_slugs():
        if not slug.startswith("location/"):
            continue
        for item in lib.load_section(slug).items:
            match = TIME_OF_DAY_WORDS.search(item.text)
            assert not match, (
                f"'{slug}/{item.name}' text contains time-of-day word "
                f"'{match.group()}' — move it to the atmosphere section"
            )


def test_constraint_demo_present(lib):
    """The neon/night dependency showcase must survive content edits."""
    urban = lib.load_section("location/urban")
    neon = next(i for i in urban.items if i.name == "neon-highway")
    assert "night" in neon.requires
    assert neon.negative == "daylight"


# -- OverDrive conversion ----------------------------------------------------

OVERDRIVE_TEMPLATES = (
    "overdrive/action",
    "overdrive/car-design",
    "overdrive/full-shot",
    "overdrive/paintshop",
    "overdrive/scenery",
)


def test_overdrive_templates_present(lib):
    assert set(OVERDRIVE_TEMPLATES) <= set(lib.template_slugs())


def test_overdrive_group_weights_uniform(lib):
    """Weights preserve the original nested-brace draw: every tag group in a
    converted section carries the same total weight."""
    for slug in ("car/color/paint", "car/design-base", "scenery/day"):
        section = lib.load_section(slug)
        totals = {}
        for item in section.items:
            assert item.tags, f"{slug}/{item.name} lost its group tag"
            totals[item.tags[0]] = totals.get(item.tags[0], 0.0) + item.weight
        values = list(totals.values())
        assert max(values) - min(values) < 0.01, (slug, totals)


def test_overdrive_label_expansion_end_to_end(lib):
    tpl = lib.load_template("overdrive/car-design")
    resolved = resolve_template(
        lib, tpl, seed=1, mode="as configured", selection={}, variables={}
    )
    out = render(resolved, tpl.render.format, tpl.render)
    assert "(HycadeBodykit style aggressive wide body kit:1.1)" in out.positive
    assert "(sleek 'Overdrive' license plate:1.2)" in out.positive
    custom = resolve_template(
        lib,
        tpl,
        seed=1,
        mode="as configured",
        selection={},
        variables={"trigger": "MyKit", "plate": "MRLN"},
    )
    custom_out = render(custom, tpl.render.format, tpl.render)
    assert "MyKit style" in custom_out.positive
    assert "'MRLN' license plate" in custom_out.positive


def test_overdrive_scenery_variants_couple_day_night(lib):
    tpl = lib.load_template("overdrive/scenery")
    seen = set()
    for seed in range(20):
        resolved = resolve_template(
            lib, tpl, seed=seed, mode="as configured", selection={}, variables={}
        )
        seen.add(resolved.variant)
        expected = (
            ("scenery/night", "scenery/light-night")
            if resolved.variant == "night"
            else ("scenery/day", "scenery/light-day")
        )
        for s in resolved.slots:
            assert s.section_slug in expected, (resolved.variant, s.section_slug)
    assert seen == {"day", "night"}


def test_overdrive_graphics_wildcards_expand(lib):
    tpl = lib.load_template("overdrive/paintshop")
    hit = False
    for seed in range(30):
        resolved = resolve_template(
            lib,
            tpl,
            seed=seed,
            mode="as configured",
            selection={"graphics": "random"},
            variables={},
        )
        graphics = next(s for s in resolved.slots if s.id == "graphics")
        if graphics.item_name is not None:
            assert "{" not in graphics.text and "|" not in graphics.text
            hit = True
    assert hit
