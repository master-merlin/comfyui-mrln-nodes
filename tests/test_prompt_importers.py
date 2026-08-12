"""Migration on-ramps: a wildcard folder and an A1111 styles.csv become
USER-tier library content.

Every fixture is written by the test that needs it — a wildcard folder off the
internet is exactly the kind of input that must not rot into a committed
binary blob. The interesting assertions are the honest ones: what the plan
promises about imported syntax is checked against what the engine actually
does with it (a `{a|b}` line really renders a choice, a `__name__` line really
does not resolve), because a false reassurance in the plan would be worse than
no warning at all.
"""

import json
import sys
from pathlib import Path

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_roots

import mrln.promptlib as pl
from mrln.promptapi import importers
from mrln.promptlib import Library

# -- fixtures ----------------------------------------------------------------

COLORS_TXT = """\
# favourite colours

red
deep blue
3::rare gold
2.5::  spaced weight
"""

# one line per verbatim-import warning class, so one section exercises all four
TOP_TXT = """\
shirt
__fabric__ blouse
{silk|linen} tunic
a {mystery} coat
half { open jacket
"""

PROPS_YAML = """\
props:
  handheld:
    - lantern
    - "2::rare orb"
  mounted: sconce
"""

STYLES_CSV = (
    "name,prompt,negative_prompt\n"
    'Cinematic,"cinematic still, {prompt}, film grain, shallow depth of field",'
    '"blurry, cartoon"\n'
    'Quality Tags,"masterpiece, best quality, highly detailed","lowres, worst quality"\n'
    'Two Lines,"first line, tags\nsecond line, more tags","bad hands"\n'
    'Choicy,"{gold|silver} trim, {prompt}",""\n'
    'Cinematic,"a duplicate name that must lose",""\n'
    'Negative Only,"","only a negative"\n'
    "\n"
)


@pytest.fixture()
def lib(tmp_path):
    """Factory + empty-ish user tier, the same fixture library the rest of the
    suite uses (so 'never touches factory' is asserted against real content)."""
    return Library(*build_roots(tmp_path / "lib"))


def write(path, text, *, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
    return path


@pytest.fixture()
def folder(tmp_path):
    """A nested wildcard folder: weights, comments, blank lines, a spacey and
    mixed-case filename, plus one file whose lines cover every warning class."""
    root = tmp_path / "wildcards"
    write(root / "colors.txt", COLORS_TXT)
    write(root / "clothing" / "top.txt", TOP_TXT)
    write(root / "Sci-Fi Weapons.txt", "plasma rifle\nrailgun\n")
    return root


@pytest.fixture()
def styles_csv(tmp_path):
    """BOM-prefixed, quoted fields carrying commas and a newline, a duplicate
    name, a negative-only row and a trailing blank row."""
    return write(tmp_path / "styles.csv", "﻿" + STYLES_CSV)


def snapshot(root):
    """path -> bytes for every file under `root` (tier-isolation evidence)."""
    root = Path(root)
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def user_files(lib):
    """Every user-tier file. The fixture library ships a few of its own, so
    'wrote nothing' always means 'unchanged', never 'empty'."""
    return snapshot(lib.user_root) if lib.user_root and lib.user_root.is_dir() else {}


def warned(report, needle):
    return [w for w in report["warnings"] if needle in w]


def one_warning(report, needle):
    hits = warned(report, needle)
    assert len(hits) == 1, f"expected exactly one warning containing {needle!r}, got {hits}"
    return hits[0]


# -- wildcards: plan, content, weights ---------------------------------------


def test_wildcard_dry_run_plans_everything_and_writes_nothing(lib, folder):
    before = user_files(lib)
    report = importers.import_wildcards(lib, str(folder), dry_run=True)
    assert report["dry_run"] is True
    assert {(w["kind"], w["slug"]) for w in report["written"]} == {
        ("section", "wildcards/clothing/top"),
        ("section", "wildcards/colors"),
        ("section", "wildcards/sci-fi-weapons"),
    }
    assert all(w["overwrites"] is False for w in report["written"])
    assert all(w["extends_factory"] is False for w in report["written"])
    assert report["skipped"] == []
    assert report["planned_files"] == 3
    assert user_files(lib) == before  # not one byte on disk
    assert lib.tier_of("sections", "wildcards/colors") == ""


def test_wildcard_txt_lines_weights_and_comments(lib, folder):
    importers.import_wildcards(lib, str(folder))
    section = lib.load_section("wildcards/colors")
    assert [(i.name, i.text, i.weight) for i in section.items] == [
        ("red", "red", 1.0),
        ("deep-blue", "deep blue", 1.0),
        ("rare-gold", "rare gold", 3.0),
        ("spaced-weight", "spaced weight", 2.5),
    ]
    # the '#' line and the blank line produced nothing at all
    assert not any("favourite" in i.text for i in section.items)


def test_wildcard_filename_case_and_spaces_slugify_but_keep_the_label(lib, folder):
    importers.import_wildcards(lib, str(folder))
    section = lib.load_section("wildcards/sci-fi-weapons")
    # the original filename survives as the label — slugification loses case
    # and spaces, and throwing that away would be the silent part of a mangle
    assert section.label == "Sci-Fi Weapons"
    assert {i.text for i in section.items} == {"plasma rifle", "railgun"}


def test_wildcard_nested_path_becomes_a_nested_slug(lib, folder):
    importers.import_wildcards(lib, str(folder))
    # 'clothing/top.txt' lands where the user's own '__clothing/top__' says
    assert lib.tier_of("sections", "wildcards/clothing/top") == "user"


# -- wildcards: the four verbatim-import warning classes ----------------------


def test_wildcard_warnings_tell_working_syntax_from_broken(lib, folder):
    report = importers.import_wildcards(lib, str(folder), dry_run=True)
    nested = one_warning(report, "__fabric__")
    assert "does NOT resolve" in nested or "NOT resolve" in nested
    assert "verbatim" in nested
    inline = one_warning(report, "inline {a|b} choices")
    assert "IS MRLN's own" in inline and "work" in inline
    # the two claims must not be confusable with each other
    assert "NOT resolve" not in inline
    variable = one_warning(report, "brace placeholder(s) without alternatives")
    assert "{mystery}" in variable and "render fails" in variable
    broken = one_warning(report, "cannot parse")
    assert "half { open jacket" in broken


def test_wildcard_verbatim_claims_match_what_the_engine_does(lib, folder):
    """The plan's promises, checked against the renderer instead of trusted."""
    importers.import_wildcards(lib, str(folder))
    ref = "wildcards/clothing/top"
    # {a|b}: promised to work -> it draws one of the alternatives
    tunic = pl.resolve_section(lib, ref, "silk-linen-tunic", seed=7).text
    assert tunic in ("silk tunic", "linen tunic")
    # __name__: promised NOT to resolve -> it renders as literal text
    assert pl.resolve_section(lib, ref, "fabric-blouse", seed=7).text == "__fabric__ blouse"
    # {name}: promised to fail without a matching variable
    with pytest.raises(pl.UnknownVariableError):
        pl.resolve_section(lib, ref, "a-mystery-coat", seed=7)
    # unparseable braces: promised to fail until edited
    with pytest.raises(pl.WildcardSyntaxError):
        pl.resolve_section(lib, ref, "half-open-jacket", seed=7)


# -- wildcards: YAML ---------------------------------------------------------


def test_wildcard_yaml_mapping_becomes_nested_slugs(lib, tmp_path):
    pytest.importorskip("yaml")
    root = tmp_path / "yaml-wild"
    write(root / "props.yaml", PROPS_YAML)
    report = importers.import_wildcards(lib, str(root))
    assert {(w["kind"], w["slug"]) for w in report["written"]} == {
        ("section", "wildcards/props/handheld"),
        ("section", "wildcards/props/mounted"),
    }
    handheld = lib.load_section("wildcards/props/handheld")
    assert [(i.text, i.weight) for i in handheld.items] == [
        ("lantern", 1.0),
        ("rare orb", 2.0),  # the N::text weight syntax works inside YAML too
    ]
    # a scalar leaf is a one-item section, not an error
    assert [i.text for i in lib.load_section("wildcards/props/mounted").items] == ["sconce"]


def test_wildcard_yaml_folder_prefixes_the_key_path_not_the_file_stem(lib, tmp_path):
    pytest.importorskip("yaml")
    root = tmp_path / "yaml-sub"
    write(root / "packs" / "fantasy.yaml", "weapons:\n  - sword\n")
    report = importers.import_wildcards(lib, str(root))
    # the folder namespaces it ('packs'), the file stem does not ('fantasy'):
    # YAML keys ARE the wildcard namespace in the ecosystem this imports from
    assert [w["slug"] for w in report["written"]] == ["wildcards/packs/weapons"]


def test_wildcard_yaml_bad_shapes_warn_and_do_not_refuse_the_folder(lib, tmp_path):
    pytest.importorskip("yaml")
    root = tmp_path / "yaml-mixed"
    write(root / "list.yaml", "- just\n- a list\n")
    write(root / "torn.yaml", "key: [unclosed\n")
    write(root / "ok.yaml", "good:\n  - fine\n")
    report = importers.import_wildcards(lib, str(root))
    assert [w["slug"] for w in report["written"]] == ["wildcards/good"]
    assert warned(report, "list.yaml") and warned(report, "torn.yaml")


def test_wildcard_yaml_without_pyyaml_is_actionable(lib, tmp_path, monkeypatch):
    root = tmp_path / "yaml-only"
    write(root / "props.yaml", PROPS_YAML)
    before = user_files(lib)
    monkeypatch.setitem(sys.modules, "yaml", None)  # 'import yaml' now raises
    status, body = importers.handle_import_wildcards(lib, {"path": str(root)})
    assert status == 400
    assert "PyYAML" in body["error"]
    assert "pip install PyYAML" in body["remediation"]
    assert user_files(lib) == before  # nothing written on the way out
    assert lib.tier_of("sections", "wildcards/props/handheld") == ""


# -- wildcards: slug hazards -------------------------------------------------


def test_derive_slug_refuses_windows_hazards_and_rescues_the_rest():
    # trailing dots and case/space noise are normalized (documented, lossless
    # enough that the label keeps the original)
    assert importers.derive_slug("wildcards", ["Stuff."]) == "wildcards/stuff"
    assert importers.derive_slug("wildcards", ["Sci-Fi Weapons"]) == "wildcards/sci-fi-weapons"
    # device names cannot be rescued: renaming 'con' to 'con-' would be a
    # silent mangle, so the caller must report it instead
    for hostile in ("con", "NUL", "com1", "aux.txt"):
        with pytest.raises(pl.SchemaError):
            importers.derive_slug("wildcards", [hostile])
    with pytest.raises(pl.SchemaError):
        importers.derive_slug("wildcards", ["___"])


def test_wildcard_unslugifiable_filename_warns_instead_of_writing(lib, tmp_path):
    root = tmp_path / "hostile"
    write(root / "___.txt", "something\n")
    write(root / "keep.txt", "kept\n")
    report = importers.import_wildcards(lib, str(root))
    assert [w["slug"] for w in report["written"]] == ["wildcards/keep"]
    assert "___.txt" in one_warning(report, "___.txt")
    # and nothing was invented under a neighbouring name
    assert [s for s in lib.section_slugs() if s.startswith("wildcards/")] == ["wildcards/keep"]


def test_wildcard_two_files_claiming_one_slug_skip_the_loser(lib, tmp_path):
    root = tmp_path / "dupes"
    write(root / "a b.txt", "first\n")
    write(root / "a-b.txt", "second\n")
    report = importers.import_wildcards(lib, str(root))
    assert [w["slug"] for w in report["written"]] == ["wildcards/a-b"]
    assert "already claimed" in one_warning(report, "already claimed")
    # exactly one of the two won, and its content is intact
    assert [i.text for i in lib.load_section("wildcards/a-b").items] == ["first"]


# -- wildcards: re-import honesty --------------------------------------------


def test_wildcard_reimport_is_identical_then_reports_the_overwrite(lib, folder):
    importers.import_wildcards(lib, str(folder))
    again = importers.import_wildcards(lib, str(folder))
    assert again["written"] == []
    assert {s["reason"] for s in again["skipped"]} == {"identical"}
    assert "needs_overwrite" not in again

    (folder / "colors.txt").write_text(COLORS_TXT + "cyan\n", encoding="utf-8")
    plan = importers.import_wildcards(lib, str(folder), dry_run=True)
    changed = [s for s in plan["skipped"] if s["slug"] == "wildcards/colors"]
    assert changed == [{"kind": "section", "slug": "wildcards/colors", "reason": "exists"}]
    assert plan["needs_overwrite"] is True
    assert not any(i.text == "cyan" for i in lib.load_section("wildcards/colors").items)

    forced = importers.import_wildcards(lib, str(folder), overwrite=True)
    assert [w for w in forced["written"] if w["slug"] == "wildcards/colors"] == [
        {
            "kind": "section",
            "slug": "wildcards/colors",
            "overwrites": True,
            "extends_factory": False,
        }
    ]
    assert any(i.text == "cyan" for i in lib.load_section("wildcards/colors").items)


# -- wildcards: refused input, walk limits -----------------------------------


def test_wildcard_bad_paths_fail_cleanly(lib, tmp_path, folder):
    before = user_files(lib)
    status, body = importers.handle_import_wildcards(lib, {})
    assert status == 400 and "path" in body["error"] and body["remediation"]

    status, body = importers.handle_import_wildcards(lib, {"path": str(tmp_path / "nope")})
    assert status == 404 and "nothing exists" in body["error"]

    status, body = importers.handle_import_wildcards(lib, {"path": str(folder / "colors.txt")})
    assert status == 400 and "not a folder" in body["error"]

    root = Path(tmp_path.anchor or "/")
    status, body = importers.handle_import_wildcards(lib, {"path": str(root)})
    assert status == 400 and "filesystem root" in body["error"]

    empty = tmp_path / "empty"
    empty.mkdir()
    status, body = importers.handle_import_wildcards(lib, {"path": str(empty)})
    assert status == 404 and "no importable wildcard file" in body["error"]
    assert user_files(lib) == before


def test_wildcard_file_count_limit_refuses_before_writing(lib, folder, monkeypatch):
    before = user_files(lib)
    monkeypatch.setattr(importers, "MAX_WILDCARD_FILES", 1)
    status, body = importers.handle_import_wildcards(lib, {"path": str(folder)})
    assert status == 400
    assert "more than 1 wildcard files" in body["error"]
    assert body["remediation"]
    assert user_files(lib) == before


def test_wildcard_entry_and_byte_limits_refuse(lib, folder, monkeypatch):
    before = user_files(lib)
    monkeypatch.setattr(importers, "MAX_DIR_ENTRIES", 1)
    status, body = importers.handle_import_wildcards(lib, {"path": str(folder)})
    assert status == 400 and "entries" in body["error"]
    monkeypatch.setattr(importers, "MAX_DIR_ENTRIES", 20_000)
    monkeypatch.setattr(importers, "MAX_WILDCARD_BYTES", 4)
    status, body = importers.handle_import_wildcards(lib, {"path": str(folder)})
    assert status == 400 and "add up to more than" in body["error"]
    assert user_files(lib) == before


def test_wildcard_depth_and_oversize_limits_are_warnings(lib, tmp_path, monkeypatch):
    root = tmp_path / "deep"
    write(root / "shallow.txt", "here\n")
    write(root / "a" / "b" / "deep.txt", "too deep\n")
    write(root / "huge.txt", "x" * 64)
    monkeypatch.setattr(importers, "MAX_WALK_DEPTH", 1)
    monkeypatch.setattr(importers, "MAX_FILE_BYTES", 32)
    report = importers.import_wildcards(lib, str(root), dry_run=True)
    assert [w["slug"] for w in report["written"]] == ["wildcards/shallow"]
    assert warned(report, "level import limit")
    assert warned(report, "per-file limit")


def test_wildcard_symlinked_folder_is_never_followed(lib, tmp_path):
    root = tmp_path / "linked"
    write(root / "real.txt", "real\n")
    try:
        (root / "loop").symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/user cannot create directory symlinks")
    report = importers.import_wildcards(lib, str(root), dry_run=True)
    assert [w["slug"] for w in report["written"]] == ["wildcards/real"]
    assert warned(report, "symlinks and")


# -- the shared contract with the bundle importer ----------------------------


def test_plan_shape_matches_the_bundle_importer(lib, tmp_path, folder, styles_csv):
    """The whole point of the reuse: both importers answer with the key set
    promptlib.import_bundle answers with, so the Composer's plan card needs no
    new UI. Verified against a real bundle plan, not against a copied list."""
    lib.save_user("sections", "mood", {"items": [{"name": "epic", "text": "epic composition"}]})
    lib.save_user("templates", "share-me", {"label": "Share Me", "slots": []})
    bundle = pl.export_bundle(lib, "templates", "share-me")
    dest = Library(*build_roots(tmp_path / "dest"))
    bundle_plan = pl.import_bundle(dest, bundle, dry_run=True)

    for report in (
        importers.import_wildcards(lib, str(folder), dry_run=True),
        importers.import_styles(lib, str(styles_csv), dry_run=True),
    ):
        missing = set(bundle_plan) - set(report) - {"template_slug", "needs_overwrite"}
        assert missing == set(), f"plan shape drifted from import_bundle: {missing}"
        for entry in report["written"]:
            assert {"kind", "slug"} <= set(entry)
            assert entry["kind"] in ("section", "template")
            flag = "extends_factory" if entry["kind"] == "section" else "shadows_factory"
            assert flag in entry
        for entry in report["skipped"]:
            assert set(entry) == {"kind", "slug", "reason"}
            assert entry["reason"] in ("identical", "exists")
        assert isinstance(report["warnings"], list)
        assert all(isinstance(w, str) for w in report["warnings"])


def test_nothing_is_written_until_every_draft_parses(lib):
    before = user_files(lib)
    drafts = [
        ("sections", "wildcards/fine", {"items": [{"name": "a", "text": "fine"}]}),
        ("sections", "wildcards/torn", {"items": "not-a-list"}),
    ]
    with pytest.raises(pl.SchemaError):
        importers.apply_drafts(lib, drafts, [], source="unit")
    assert lib.tier_of("sections", "wildcards/fine") == ""  # the good one too
    assert user_files(lib) == before


def test_imports_only_ever_touch_the_user_tier(lib, folder, styles_csv):
    factory_before = snapshot(lib.factory_root)
    source_before = snapshot(folder)
    csv_before = styles_csv.read_bytes()

    wildcards = importers.import_wildcards(lib, str(folder))
    styles = importers.import_styles(lib, str(styles_csv))

    assert snapshot(lib.factory_root) == factory_before  # factory is read-only
    assert snapshot(folder) == source_before  # the source folder is read-only
    assert styles_csv.read_bytes() == csv_before
    written = [*wildcards["written"], *styles["written"]]
    assert written
    for entry in written:
        kind = "sections" if entry["kind"] == "section" else "templates"
        path = lib.user_root / kind / f"{entry['slug']}.json"
        assert path.is_file()
        assert lib.user_root.resolve() in path.resolve().parents
        assert lib.tier_of(kind, entry["slug"]) == "user"
    # every file the import created lives under the user root, nowhere else
    assert set(user_files(lib)) >= {
        f"sections/{e['slug']}.json" for e in written if e["kind"] == "section"
    }


# -- styles.csv --------------------------------------------------------------


def test_styles_placeholder_row_becomes_a_template(lib, styles_csv):
    importers.import_styles(lib, str(styles_csv))
    raw = json.loads(
        (lib.user_root / "templates" / "styles" / "cinematic.json").read_text(encoding="utf-8")
    )
    assert raw["label"] == "Cinematic"
    assert raw["prefix"] == "cinematic still"  # text before {prompt}
    assert raw["suffix"] == "film grain, shallow depth of field"  # text after it
    assert raw["negative"] == "blurry, cartoon"  # the negative column
    tpl = lib.load_template("styles/cinematic")
    out = pl.compose(lib, tpl, seed=3, mode="as configured", selection={}, variables={})
    assert out.rendered.positive == "cinematic still, film grain, shallow depth of field"
    assert out.rendered.negative == "blurry, cartoon"


def test_styles_plain_rows_become_items_in_one_section(lib, styles_csv):
    importers.import_styles(lib, str(styles_csv))
    section = lib.load_section(importers.STYLES_SECTION)
    by_name = {i.name: i for i in section.items}
    assert set(by_name) == {"quality-tags", "two-lines"}
    assert by_name["quality-tags"].text == "masterpiece, best quality, highly detailed"
    assert by_name["quality-tags"].negative == "lowres, worst quality"
    # a quoted field carrying commas AND a newline stays ONE field
    assert by_name["two-lines"].text == "first line, tags\nsecond line, more tags"
    assert by_name["two-lines"].negative == "bad hands"


def test_styles_bom_duplicate_and_negative_only_rows(lib, styles_csv):
    report = importers.import_styles(lib, str(styles_csv), dry_run=True)
    # the BOM did not become part of the 'name' header (else nothing parses)
    assert {(w["kind"], w["slug"]) for w in report["written"]} == {
        ("template", "styles/cinematic"),
        ("template", "styles/choicy"),
        ("section", "styles/a1111"),
    }
    duplicate = one_warning(report, "already imported from line")
    assert "Cinematic" in duplicate and "the first one wins" in duplicate
    assert "Negative Only" in one_warning(report, "prompt column is empty")


def test_styles_inline_choice_warning_is_true(lib, styles_csv):
    report = importers.import_styles(lib, str(styles_csv))
    assert "styles/choicy" in one_warning(report, "inline {a|b} choices")
    tpl = lib.load_template("styles/choicy")
    out = pl.compose(lib, tpl, seed=11, mode="as configured", selection={}, variables={})
    assert out.rendered.positive in ("gold trim", "silver trim")


def test_styles_semicolon_dialect_is_sniffed(lib, tmp_path):
    path = write(
        tmp_path / "euro.csv",
        "name;prompt;negative_prompt\nDramatic;dramatic lighting, {prompt};flat\n",
    )
    report = importers.import_styles(lib, str(path))
    assert [w["slug"] for w in report["written"]] == ["styles/dramatic"]
    assert lib.load_template("styles/dramatic").prefix == "dramatic lighting"
    assert warned(report, "separated by")


def test_styles_negative_placeholder_is_reported_not_silently_kept(lib, tmp_path):
    path = write(
        tmp_path / "negph.csv",
        'name,prompt,negative_prompt\nOdd,"{prompt}, extra","no {prompt} here"\n',
    )
    report = importers.import_styles(lib, str(path))
    assert "{prompt}" in one_warning(report, "keeps a '{prompt}' marker")
    assert lib.load_template("styles/odd").negative == "no {prompt} here"


def test_styles_unreadable_header_is_refused(lib, tmp_path):
    before = user_files(lib)
    path = write(tmp_path / "junk.csv", "alpha,beta\n1,2\n")
    status, body = importers.handle_import_styles(lib, {"path": str(path)})
    assert status == 400
    assert "no recognizable header" in body["error"]
    assert "name,prompt,negative_prompt" in body["remediation"]
    assert user_files(lib) == before


def test_styles_empty_and_headers_only_files_are_refused(lib, tmp_path):
    blank = write(tmp_path / "blank.csv", "   \n")
    status, body = importers.handle_import_styles(lib, {"path": str(blank)})
    assert status == 400 and "empty" in body["error"]
    header = write(tmp_path / "header.csv", "name,prompt,negative_prompt\n")
    status, body = importers.handle_import_styles(lib, {"path": str(header)})
    assert status == 400 and "no style rows" in body["error"]


def test_styles_slug_hostile_name_warns_instead_of_writing(lib, tmp_path):
    path = write(
        tmp_path / "hostile.csv",
        'name,prompt,negative_prompt\n"...","dots, {prompt}",""\nGood,"ok, {prompt}",""\n',
    )
    report = importers.import_styles(lib, str(path))
    assert [w["slug"] for w in report["written"]] == ["styles/good"]
    assert warned(report, "rename the style")
    assert [s for s in lib.template_slugs() if s.startswith("styles/")] == ["styles/good"]


def test_styles_dry_run_writes_nothing_and_reimport_reports_overwrite(lib, styles_csv):
    before = user_files(lib)
    plan = importers.import_styles(lib, str(styles_csv), dry_run=True)
    assert plan["dry_run"] is True and plan["written"]
    assert user_files(lib) == before

    importers.import_styles(lib, str(styles_csv))
    again = importers.import_styles(lib, str(styles_csv))
    assert again["written"] == [] and {s["reason"] for s in again["skipped"]} == {"identical"}

    write(styles_csv, "﻿" + STYLES_CSV.replace("film grain", "heavy grain"))
    plan = importers.import_styles(lib, str(styles_csv), dry_run=True)
    assert {"kind": "template", "slug": "styles/cinematic", "reason": "exists"} in plan["skipped"]
    assert plan["needs_overwrite"] is True
    assert "film grain" in lib.load_template("styles/cinematic").suffix

    forced = importers.import_styles(lib, str(styles_csv), overwrite=True)
    assert {
        "kind": "template",
        "slug": "styles/cinematic",
        "overwrites": True,
        "shadows_factory": False,
    } in forced["written"]
    assert "heavy grain" in lib.load_template("styles/cinematic").suffix


def test_styles_utf16_export_and_cp1252_fallback(lib, tmp_path):
    utf16 = write(
        tmp_path / "u16.csv",
        "name,prompt,negative_prompt\nCafé,café lighting\n",
        encoding="utf-16",
    )
    report = importers.import_styles(lib, str(utf16))
    assert [w["slug"] for w in report["written"]] == ["styles/a1111"]
    assert warned(report, "UTF-16")
    latin = write(
        tmp_path / "cp.csv",
        "name,prompt,negative_prompt\nCafé,café lighting\n",
        encoding="cp1252",
    )
    report = importers.import_styles(lib, str(latin))
    assert warned(report, "Windows-1252")


# -- endpoints ---------------------------------------------------------------


def test_endpoints_answer_with_a_fingerprint_and_status_200(lib, folder, styles_csv):
    status, report = importers.handle_import_wildcards(lib, {"path": str(folder), "dry_run": True})
    assert status == 200 and report["dry_run"] is True and report["fingerprint"]
    status, report = importers.handle_import_styles(lib, {"path": str(styles_csv)})
    assert status == 200 and report["fingerprint"] == lib.fingerprint()
    assert report["source"] == str(styles_csv)
