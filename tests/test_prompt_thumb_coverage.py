"""THE RULE (user 2026-08-12): no factory section or template ships without a
thumbnail.

Why it is a test and not a habit: the browse grid draws a domain glyph for
anything with no tile, and a grid that is half pictures and half glyphs reads
as BROKEN rather than as curated — the missing tiles look like a loading
failure, not like a deliberate subset. The failure mode is also silent, because
adding a section is a one-file change that nothing else objects to. So the
suite objects.

Fixing a failure here means running the renderer, not deleting the assertion:

    python _harness/tools/render_thumbs.py            # everything missing
    python _harness/tools/render_thumbs.py --only <slug> --force

These tests read the FACTORY tier directly rather than through
`thumbs.has_thumb`, which would also accept a user-tier tile — a thumbnail in
somebody's user library is not a shipped one.
"""

import pytest
import support  # noqa: F401

from mrln import promptlib as pl
from mrln.promptlib import store

# The repo is cloned by every user and thumbnails are its only binary content,
# so the set is budgeted rather than left to grow.
#
# RAISED 2 MB -> 3 MB (2026-08-13), and deliberately not quietly: §6.1 sized
# 2 MB for "57 templates + CURATED sections", and the rule that every factory
# entry ships a tile changed the set from ~100 to 268. At 7.3 KiB average and a
# 6.8 KiB median the tiles are already lean — the number that grew is the count,
# which is the rule working, not a regression. The alternative was a second
# lossy webp pass over pristine renders to buy ~300 KiB, which trades the thing
# users see for headroom nobody needs.
#
# PER-FILE is the cap that actually protects a clone: total size scales with a
# library that is meant to grow, but one pathological tile is always a mistake.
BUDGET_BYTES = 3 * 1024 * 1024
MAX_TILE_BYTES = 48 * 1024  # observed worst case is 23 KiB


@pytest.fixture(scope="module")
def factory():
    """The shipped library, with NO user tier — the tier under test."""
    return pl.Library(factory_root=pl.default_roots()[0], user_root=None)


def factory_thumb_path(kind, slug):
    return pl.default_roots()[0] / "thumbs" / kind / f"{slug}{store.THUMB_EXT}"


def missing(kind, slugs):
    return sorted(slug for slug in slugs if not factory_thumb_path(kind, slug).is_file())


def test_every_factory_template_has_a_thumbnail(factory):
    gap = missing("templates", factory.template_slugs())
    assert not gap, (
        f"{len(gap)} template(s) ship without a thumbnail: {gap[:8]}"
        f"{' and more' if len(gap) > 8 else ''}\n"
        "Render them: python _harness/tools/render_thumbs.py"
    )


def test_every_factory_section_has_a_thumbnail(factory):
    gap = missing("sections", factory.section_slugs())
    assert not gap, (
        f"{len(gap)} section(s) ship without a thumbnail: {gap[:8]}"
        f"{' and more' if len(gap) > 8 else ''}\n"
        "Render them: python _harness/tools/render_thumbs.py"
    )


def test_no_orphan_thumbnails(factory):
    """A tile whose section or template is gone is dead weight in every clone —
    and a rename that left one behind is exactly how a stale picture ends up
    next to the wrong name."""
    known = {
        "templates": set(factory.template_slugs()),
        "sections": set(factory.section_slugs()),
    }
    orphans = []
    for kind, slugs in known.items():
        root = pl.default_roots()[0] / "thumbs" / kind
        if not root.is_dir():
            continue
        for path in root.rglob(f"*{store.THUMB_EXT}"):
            slug = path.relative_to(root).with_suffix("").as_posix()
            if slug not in slugs:
                orphans.append(f"{kind}/{slug}")
    assert not orphans, f"thumbnail(s) with no library entry: {sorted(orphans)[:8]}"


def test_the_shipped_thumbnail_set_stays_inside_its_budget():
    root = pl.default_roots()[0] / "thumbs"
    files = list(root.rglob(f"*{store.THUMB_EXT}")) if root.is_dir() else []
    total = sum(path.stat().st_size for path in files)
    assert total <= BUDGET_BYTES, (
        f"the factory thumbnail set is {total / 1024:.0f} KiB, over the "
        f"{BUDGET_BYTES / 1024:.0f} KiB budget ({len(files)} files). Re-render at a "
        "lower quality (thumbs.THUMB_QUALITY) before raising this — and if you do "
        "raise it, say why here; every user clones these bytes."
    )


def test_no_single_thumbnail_is_oversized():
    """The total scales with a library meant to grow; one huge tile never has
    to. A 256 px webp that lands far above the ~7 KiB norm means the render was
    noise rather than a picture, which is worth catching by itself."""
    root = pl.default_roots()[0] / "thumbs"
    fat = [
        f"{path.relative_to(root).as_posix()} ({path.stat().st_size / 1024:.0f} KiB)"
        for path in (root.rglob(f"*{store.THUMB_EXT}") if root.is_dir() else [])
        if path.stat().st_size > MAX_TILE_BYTES
    ]
    assert not fat, (
        f"thumbnail(s) over {MAX_TILE_BYTES / 1024:.0f} KiB: {fat[:6]} — re-roll them "
        "(--only <slug> --force); a tile this size is usually noise, not detail"
    )


def test_thumbnails_are_the_webp_the_api_serves():
    """The endpoint answers image/webp with no conversion step, so a stray PNG
    would be served under the wrong content type."""
    root = pl.default_roots()[0] / "thumbs"
    if not root.is_dir():
        pytest.skip("no factory thumbnails rendered yet")
    strays = [
        path.name
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() != store.THUMB_EXT
    ]
    assert not strays, f"non-{store.THUMB_EXT} files in the thumbnail tree: {strays[:8]}"
