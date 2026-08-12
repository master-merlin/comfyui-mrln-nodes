"""Regression pins for the pre-shipment audit findings in mrln/promptlib.

Every test here fails against the pre-fix engine. Grouped by the file the
defect lived in; each docstring names the behaviour that must not come back.
"""

import dataclasses
import inspect
import json
import logging
from importlib import import_module

import pytest
import support  # noqa: F401

from mrln.promptlib import (
    Library,
    PromptLibError,
    RenderError,
    SchemaError,
    SelectionError,
    TemplateNotFoundError,
    parse_section,
    parse_template,
    render,
    resolve_template,
    validate_slug,
)
from mrln.promptlib import library as liblib
from mrln.promptlib import resolve as resolvelib
from mrln.promptlib import schema as schemalib
from mrln.promptlib.profiles import _RENDER_OVERRIDES, _effective_render
from mrln.promptlib.schema import RenderConfig

# `mrln.promptlib.render` resolves to the re-exported render() FUNCTION, so
# reach the module through sys.modules to assert on its globals.
renderlib = import_module("mrln.promptlib.render")


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _lib(tmp_path, sections, templates=None):
    factory = tmp_path / "factory"
    for slug, data in sections.items():
        _write(factory, f"sections/{slug}.json", data)
    for slug, data in (templates or {}).items():
        _write(factory, f"templates/{slug}.json", data)
    return Library(factory, tmp_path / "user")


def _resolve(lib, tpl_slug, **kw):
    tpl = lib.load_template(tpl_slug)
    kw.setdefault("seed", 1)
    kw.setdefault("mode", "as configured")
    kw.setdefault("selection", {})
    kw.setdefault("variables", {})
    return tpl, resolve_template(lib, tpl, **kw)


# -- schema.py: slug hygiene --------------------------------------------------


@pytest.mark.parametrize("slug", ["foo.", "foo./bar", "a/b.", "x.."])
def test_slug_segment_rejects_a_trailing_dot(slug):
    """Win32 strips a trailing '.' when creating the directory, so the slug
    save_user confirmed 404s afterwards and the content lives under a name the
    caller never chose. Rejected up front instead."""
    with pytest.raises(SchemaError, match="invalid slug segment"):
        validate_slug(slug)


@pytest.mark.parametrize("slug", ["con", "nul/x", "a/com1", "lpt9", "aux.old"])
def test_slug_segment_rejects_windows_device_names(slug):
    """A file can never exist under a Win32 device name — reject before the
    write silently lands elsewhere (the same gate guards store.py thumbs)."""
    with pytest.raises(SchemaError, match="reserved Windows device name"):
        validate_slug(slug)


def test_valid_slugs_still_pass():
    for slug in ("color", "vehicle/car/paint", "v1.5", "a_b-c", "trailing-", "com", "console"):
        assert validate_slug(slug) == slug


def test_save_user_refuses_a_trailing_dot_slug(tmp_path):
    lib = _lib(tmp_path, {"color": {"items": [{"name": "red", "text": "bright red"}]}})
    with pytest.raises(SchemaError, match="invalid slug segment"):
        lib.save_user("sections", "foo./bar", {"items": [{"name": "a", "text": "a"}]})


# -- schema.py: non-finite numbers -------------------------------------------


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_item_weight_is_rejected(literal):
    """json.loads accepts the bare NaN/Infinity literals and `nan < 0` is
    False, so a NaN weight used to validate — and then made weighted_index
    always draw the LAST pool item, silently and deterministically."""
    data = json.loads(f'{{"items": [{{"name": "a", "text": "x", "weight": {literal}}}]}}')
    with pytest.raises(SchemaError, match="weight"):
        parse_section(data, "s", "f.json")


@pytest.mark.parametrize("field", ["emphasis", "empty_weight"])
def test_non_finite_slot_numbers_are_rejected(field):
    data = json.loads(f'{{"slots": [{{"id": "a", "ref": "color", "{field}": NaN}}]}}')
    with pytest.raises(SchemaError, match=field):
        parse_template(data, "t", "f.json")


def test_non_finite_lora_strength_is_rejected():
    data = json.loads(
        '{"items": [{"name": "a", "text": "x", '
        '"data": {"lora": "a.safetensors", "strength_model": NaN}}]}'
    )
    with pytest.raises(SchemaError, match="strength_model"):
        parse_section(data, "s", "f.json")


def test_nan_weight_file_fails_to_load_instead_of_biasing_draws(tmp_path):
    factory = tmp_path / "factory"
    (factory / "sections").mkdir(parents=True)
    (factory / "sections" / "color.json").write_text(
        '{"items": [{"name": "red", "text": "r", "weight": NaN}, {"name": "blue", "text": "b"}]}',
        encoding="utf-8",
    )
    with pytest.raises(SchemaError, match="weight"):
        Library(factory, None).load_section("color")


# -- schema.py: reserved names -----------------------------------------------


@pytest.mark.parametrize("name", ["off", "random", "🎲 random", "🔇 off", "random@7"])
def test_reserved_item_names_are_rejected(name):
    """resolve._parse_token reads control tokens BEFORE item lookup, so an
    item called 'off' muted its slot instead of being picked — content that
    can never be selected."""
    with pytest.raises(SchemaError, match="reserved selection token"):
        parse_section({"items": [{"name": name, "text": "x"}]}, "s", "f.json")


@pytest.mark.parametrize("name", ["random", "off"])
def test_reserved_variant_names_are_rejected(name):
    data = {"slots": [{"id": "a", "ref": "color"}], "variants": [{"name": name, "slots": []}]}
    with pytest.raises(SchemaError, match="reserved selection token"):
        parse_template(data, "t", "f.json")


def test_ordinary_names_containing_reserved_words_still_parse():
    section = parse_section(
        {"items": [{"name": "off-road", "text": "x"}, {"name": "randomizer", "text": "y"}]},
        "s",
        "f.json",
    )
    assert [i.name for i in section.items] == ["off-road", "randomizer"]


# -- schema.py: slot id namespace --------------------------------------------


def test_dotted_slot_id_is_rejected():
    """'.' separates a parent slot from its nested child in the selection
    format, so a dotted top-level id is emitted twice in a selection."""
    with pytest.raises(SchemaError, match="reserved"):
        parse_template({"slots": [{"id": "a.b", "ref": "color"}]}, "t", "f.json")


def test_dotted_child_slot_id_is_rejected():
    with pytest.raises(SchemaError, match="reserved"):
        parse_section(
            {"items": [{"name": "a", "text": "x {b.c}", "slots": [{"id": "b.c", "ref": "color"}]}]},
            "s",
            "f.json",
        )


# -- schema.py + resolve.py: template 'order' --------------------------------


def test_duplicate_order_entry_is_rejected():
    """A repeated id resolved the SAME slot twice under the same seed key —
    its text appeared verbatim twice in the prompt."""
    data = {"slots": [{"id": "a", "ref": "color"}], "order": ["a", "a"]}
    with pytest.raises(SchemaError, match="repeats"):
        parse_template(data, "t", "f.json")


def test_selection_for_an_order_omitted_slot_is_an_error(tmp_path):
    """A partial 'order' silently drops the slot AND every pick aimed at it:
    'b=blue' passed validation (b IS a shared slot) and then did nothing."""
    lib = _lib(
        tmp_path,
        {"color": {"items": [{"name": "red", "text": "r"}, {"name": "blue", "text": "b"}]}},
        {
            "partial": {
                "slots": [{"id": "a", "ref": "color"}, {"id": "b", "ref": "color"}],
                "order": ["a"],
            }
        },
    )
    _tpl, resolved = _resolve(lib, "partial")
    assert [s.id for s in resolved.slots] == ["a"]  # partial order still renders
    with pytest.raises(SelectionError, match="missing from the template's 'order'"):
        _resolve(lib, "partial", selection={"b": "blue"})


# -- resolve.py: degenerate pools and empty selections -----------------------


def test_all_zero_weight_pool_raises_instead_of_drawing_the_first_item(tmp_path):
    """weight 0 means 'never draw'. With a positive total that holds; with an
    all-zero pool seeding.weighted_index answers index 0 and the first
    weight-0 item WAS drawn."""
    lib = _lib(
        tmp_path,
        {
            "color": {
                "items": [
                    {"name": "a", "text": "A", "weight": 0},
                    {"name": "b", "text": "B", "weight": 0},
                ]
            }
        },
        {"t": {"slots": [{"id": "paint", "ref": "color"}]}},
    )
    with pytest.raises(SelectionError, match="has weight 0"):
        _resolve(lib, "t")


def test_zero_weight_pool_with_empty_weight_still_draws_empty(tmp_path):
    """allow_empty rescues the pool: the total is positive, so the slot simply
    omits instead of erroring."""
    lib = _lib(
        tmp_path,
        {"color": {"items": [{"name": "a", "text": "A", "weight": 0}]}},
        {
            "t": {
                "slots": [{"id": "paint", "ref": "color", "allow_empty": True, "empty_weight": 1.0}]
            }
        },
    )
    _tpl, resolved = _resolve(lib, "t")
    assert resolved.slots[0].item_name is None


def _empty_selection_lib(tmp_path):
    return _lib(
        tmp_path,
        {"color": {"items": [{"name": "red", "text": "R"}, {"name": "blue", "text": "B"}]}},
        {
            "t": {
                "slots": [{"id": "paint", "ref": "color", "default": "blue"}],
                "variants": [
                    {"name": "v1", "slots": [{"id": "x", "ref": "color", "default": "red"}]},
                    {"name": "v2", "slots": [{"id": "x", "ref": "color", "default": "red"}]},
                ],
                "variant_default": "v2",
            }
        },
    )


def test_empty_selection_value_falls_back_to_the_slot_default(tmp_path):
    """'paint=' is not a pick of the item named '': it used to reach the
    stale-item fallback and report the misdiagnosis "item '' is not in
    'color' (renamed or removed)". Now it matches the variant line."""
    lib = _empty_selection_lib(tmp_path)
    _tpl, resolved = _resolve(lib, "t", selection={"paint": "", "variant": ""})
    paint = next(s for s in resolved.slots if s.id == "paint")
    assert paint.item_name == "blue"  # the slot default, not a random draw
    assert paint.stale_note == ""
    assert paint.random is False
    assert resolved.variant == "v2"  # variant already behaved this way


def test_whitespace_only_selection_value_falls_back_too(tmp_path):
    lib = _empty_selection_lib(tmp_path)
    _tpl, resolved = _resolve(lib, "t", selection={"paint": "   "})
    assert next(s for s in resolved.slots if s.id == "paint").item_name == "blue"


def test_randomize_all_preserves_an_explicit_seed_pin(tmp_path):
    """Documented (resolve.MODES) and pinned here: 'randomize all' re-rolls
    everything EXCEPT a mute and an explicit 'random@N' pin — both are
    deliberate user decisions and both show in the choices report."""
    lib = _lib(
        tmp_path,
        {"color": {"items": [{"name": n, "text": n} for n in ("a", "b", "c", "d", "e", "f", "g")]}},
        {
            "t": {
                "slots": [
                    {"id": "pinned", "ref": "color", "default": "random@42"},
                    {"id": "free", "ref": "color", "default": "random"},
                    {"id": "muted", "ref": "color", "default": "off"},
                ]
            }
        },
    )
    drawn = {"pinned": set(), "free": set()}
    for seed in (1, 999, 12345):
        _tpl, resolved = _resolve(lib, "t", seed=seed, mode="randomize all")
        by_id = {s.id: s for s in resolved.slots}
        assert by_id["pinned"].seed_used == 42
        assert by_id["muted"].item_name is None  # a mute survives the mode too
        drawn["pinned"].add(by_id["pinned"].item_name)
        drawn["free"].add(by_id["free"].item_name)
    assert len(drawn["pinned"]) == 1  # the pin freezes the draw
    assert len(drawn["free"]) > 1  # everything else re-rolls


def test_tombstoned_item_as_a_fixed_default_degrades_to_a_flagged_random(tmp_path):
    """Combination pin: hiding an item in the user tier flips every template
    that PINNED it to a seeded random draw with a loud note — for the stored
    default and for a workflow selection alike."""
    factory = tmp_path / "factory"
    user = tmp_path / "user"
    _write(
        factory,
        "sections/color.json",
        {"items": [{"name": "red", "text": "R"}, {"name": "gold", "text": "G"}]},
    )
    _write(
        factory,
        "templates/t.json",
        {"slots": [{"id": "paint", "ref": "color", "default": "gold"}]},
    )
    _write(user, "sections/color.json", {"items": [{"name": "gold", "hidden": True}]})
    lib = Library(factory, user)
    for selection in ({}, {"paint": "gold"}):
        _tpl, resolved = _resolve(lib, "t", selection=selection)
        paint = resolved.slots[0]
        assert paint.item_name == "red"
        assert paint.random is True
        assert "gold" in paint.stale_note and "renamed or removed" in paint.stale_note


# -- render.py ---------------------------------------------------------------


def _conflict_lib(tmp_path, positive_text, negative):
    return _lib(
        tmp_path,
        {"color": {"items": [{"name": "only", "text": positive_text}]}},
        {"t": {"slots": [{"id": "paint", "ref": "color"}], "negative": negative}},
    )


def test_negative_conflict_needs_a_whole_word_match(tmp_path):
    """A plain substring search dropped the negative term 'art' against a
    positive 'trending on artstation' — the negative prompt was silently
    corrupted although the prompt never contains the word."""
    lib = _conflict_lib(tmp_path, "trending on artstation", "art")
    tpl, resolved = _resolve(lib, "t")
    out = render(resolved, "string", tpl.render, conflict_policy="positive prevails")
    assert out.negative == "art"
    assert "conflict:" not in out.choices


def test_negative_conflict_still_fires_on_a_real_word(tmp_path):
    lib = _conflict_lib(tmp_path, "art deco showroom", "art")
    tpl, resolved = _resolve(lib, "t")
    kept = render(resolved, "string", tpl.render)
    assert "conflict: 'art'" in kept.choices and "art" in kept.negative
    dropped = render(resolved, "string", tpl.render, conflict_policy="positive prevails")
    assert dropped.negative == ""


def test_negative_conflict_matches_a_multi_word_term_with_punctuation(tmp_path):
    """The lookaround form (not \\b) keeps terms that begin or end in
    punctuation matching."""
    lib = _conflict_lib(tmp_path, "a (blurry) shot", "(blurry)")
    tpl, resolved = _resolve(lib, "t")
    assert "conflict: '(blurry)'" in render(resolved, "string", tpl.render).choices


def test_excludes_warns_when_the_excluding_item_carries_the_tag_too(tmp_path):
    """`present - set(slot.tags)` removed the tag VALUE globally, so a slot
    that carried AND excluded 'glass' hid every other slot's 'glass'."""
    lib = _lib(
        tmp_path,
        {
            "a": {
                "items": [{"name": "one", "text": "one", "tags": ["glass"], "excludes": ["glass"]}]
            },
            "b": {"items": [{"name": "two", "text": "two", "tags": ["glass"]}]},
        },
        {"t": {"slots": [{"id": "x", "ref": "a"}, {"id": "y", "ref": "b"}]}},
    )
    tpl, resolved = _resolve(lib, "t")
    choices = render(resolved, "string", tpl.render).choices
    assert "⚠ x: excludes 'glass' — but another drawn item carries it" in choices


def test_excludes_stays_quiet_when_only_the_excluding_slot_carries_the_tag(tmp_path):
    lib = _lib(
        tmp_path,
        {
            "a": {
                "items": [{"name": "one", "text": "one", "tags": ["glass"], "excludes": ["glass"]}]
            },
            "b": {"items": [{"name": "two", "text": "two"}]},
        },
        {"t": {"slots": [{"id": "x", "ref": "a"}, {"id": "y", "ref": "b"}]}},
    )
    tpl, resolved = _resolve(lib, "t")
    assert "excludes 'glass'" not in render(resolved, "string", tpl.render).choices


def test_render_knob_errors_join_the_promptlib_hierarchy(tmp_path):
    """render() raised a bare ValueError, which slipped past every
    `except PromptLibError` guard and surfaced as a 500."""
    lib = _lib(
        tmp_path,
        {"color": {"items": [{"name": "red", "text": "R"}]}},
        {"t": {"slots": [{"id": "paint", "ref": "color"}]}},
    )
    tpl, resolved = _resolve(lib, "t")
    with pytest.raises(RenderError, match="unknown format"):
        render(resolved, "yaml", tpl.render)
    with pytest.raises(RenderError, match="unknown conflict policy"):
        render(resolved, "string", tpl.render, conflict_policy="nope")
    assert issubclass(RenderError, PromptLibError)


def test_formats_has_a_single_definition():
    """render dispatched against its own copy while parse_template validated
    against schema's — adding a format to one only would let files parse that
    render then rejects."""
    assert renderlib.FORMATS is schemalib.FORMATS


def test_emphasis_wrapping_is_one_shared_rule():
    assert renderlib.emphasize is schemalib.emphasize
    assert resolvelib.emphasize is schemalib.emphasize
    assert schemalib.emphasize("a canopy.", 1.15) == "(a canopy:1.15)"
    assert schemalib.emphasize("plain", 1.0) == "plain"
    assert schemalib.emphasize("plain", None) == "plain"
    assert schemalib.emphasize("", 1.4) == ""


def test_inline_woven_slot_matches_body_emphasis(tmp_path):
    """The three emphasis sites must stay byte-identical — pinned through the
    weaving path, which used to carry its own copy of the expression."""
    lib = _lib(
        tmp_path,
        {"color": {"items": [{"name": "red", "text": "bright red."}]}},
        {
            "t": {
                "prefix": "a {paint} car",
                "slots": [{"id": "paint", "ref": "color", "emphasis": 1.3}],
            }
        },
    )
    _tpl, resolved = _resolve(lib, "t")
    assert resolved.prefix == "a (bright red:1.3) car"


# -- library.py --------------------------------------------------------------


def test_parse_cache_keeps_exactly_one_entry_per_file(tmp_path):
    """Keyed by (path, mtime_ns), every save left the previous generation's
    parsed object in a module-level dict that nothing ever evicted."""
    lib = _lib(tmp_path, {"color": {"items": [{"name": "red", "text": "R"}]}})
    path = str((tmp_path / "factory" / "sections" / "color.json").resolve())
    liblib._PARSE_CACHE.pop(path, None)
    target = tmp_path / "factory" / "sections" / "color.json"
    for i in range(5):
        target.write_text(
            json.dumps({"items": [{"name": "red", "text": f"R{i}"}]}), encoding="utf-8"
        )
        lib.invalidate()
        assert lib.load_section("color").items[0].text == f"R{i}"
    assert sum(1 for key in liblib._PARSE_CACHE if key == path) == 1


def test_scan_skips_a_file_that_vanishes_between_rglob_and_stat(tmp_path, monkeypatch):
    """A concurrent Composer delete raised FileNotFoundError out of _scan —
    not a PromptLibError, so it took down every listing and compose as a 500."""
    lib = _lib(
        tmp_path,
        {
            "color": {"items": [{"name": "red", "text": "R"}]},
            "mood": {"items": [{"name": "epic", "text": "E"}]},
        },
    )
    doomed = str(tmp_path / "factory" / "sections" / "mood.json")
    real_stat = liblib.Path.stat

    def flaky(self, *a, **kw):
        if str(self) == doomed:  # str(), never resolve(): resolve() re-enters stat
            raise FileNotFoundError(2, "vanished", str(self))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(liblib.Path, "stat", flaky)
    assert lib.section_slugs() == ["color"]  # skipped, not crashed
    lib.invalidate()
    assert lib.fingerprint()  # the other scan entry points survive too


def test_leaf_section_shadowing_a_folder_warns(tmp_path, caplog):
    """Leaf wins (flipping that would re-point live slots), but it must not be
    silent — the folder's sections are unreachable through that ref."""
    lib = _lib(
        tmp_path,
        {
            "veh": {"items": [{"name": "leaf-item", "text": "L"}]},
            "veh/cars": {"items": [{"name": "folder-item", "text": "F"}]},
        },
    )
    liblib._SHADOW_WARNED.discard("veh")
    with caplog.at_level(logging.WARNING, logger="mrln.promptlib.library"):
        assert [q for q, _, _ in lib.scope_items("veh")] == ["leaf-item"]
    assert "veh/cars" in caplog.text and "unreachable" in caplog.text


def test_entry_carries_no_dead_kind_field():
    """Entry.kind was written on every scanned file and read by nobody."""
    assert [f.name for f in dataclasses.fields(liblib.Entry)] == [
        "slug",
        "tier",
        "path",
        "mtime_ns",
        "size",
    ]


# -- errors.py / profiles.py -------------------------------------------------


def test_template_not_found_error_has_no_dead_search_dirs_parameter():
    """No call site ever passed it, so the '(searched: …)' branch was
    unreachable."""
    assert list(inspect.signature(TemplateNotFoundError.__init__).parameters) == [
        "self",
        "slug",
        "available",
    ]
    assert "searched" not in str(TemplateNotFoundError("t", ["a"]))


def test_a_profile_cannot_override_which_profile_applies():
    """'profile' in _RENDER_OVERRIDES copied a profile's render.profile onto
    the effective config, where nothing ever read it — a silent no-op."""
    assert "profile" not in _RENDER_OVERRIDES
    over = {"render": {"profile": "b", "joiner": " | "}}
    cfg = _effective_render(RenderConfig(profile="a"), over)
    assert cfg.profile == "a" and cfg.joiner == " | "


def test_non_finite_profile_emphasis_override_is_rejected(tmp_path):
    """Pack-level profiles are raw user JSON that never sees the schema, and
    float('nan') succeeds — a NaN emphasis rendered '(text:nan)'."""
    from mrln.promptlib.profiles import apply_template_overrides

    tpl = parse_template({"slots": [{"id": "paint", "ref": "color"}]}, "t", "f.json")
    with pytest.raises(SelectionError, match="must be a number"):
        apply_template_overrides(tpl, {"slots": {"paint": {"emphasis": float("nan")}}})
