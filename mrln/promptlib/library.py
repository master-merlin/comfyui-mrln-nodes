"""Two-tier library: factory content shipped with the pack + persistent user
tier. Slug = file path relative to the kind dir, POSIX separators, no
extension. Same-slug SECTIONS compound (the user file extends factory unless
it sets '"replaces": true'); same-slug templates replace factory entirely.

Renamed slugs never just die: `aliases.json` at each tier root maps old slug
-> new slug, consulted only when a lookup misses. A factory restructure ships
aliases for every renamed slug so existing user templates keep loading.
"""

import contextlib
import hashlib
import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import SchemaError, SectionNotFoundError, TemplateNotFoundError
from .schema import SLUG_SEGMENT_RE, Section, default_label, parse_section, parse_template

KINDS = ("sections", "templates", "profiles", "system_prompts")
WRITABLE_KINDS = ("sections", "templates")

_log = logging.getLogger(__name__)
_SHADOW_WARNED: set = set()  # refs already reported as leaf-shadows-folder


# Win32 resolves these names (with or without an extension) to devices, so a
# file can never be created under them — reject before the write silently
# lands somewhere else.
_WIN_DEVICE_NAMES = (
    frozenset({"con", "prn", "aux", "nul"})
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def validate_slug(slug):
    """Path-safe slug or SchemaError. Every '/'-segment must match
    SLUG_SEGMENT_RE — this is the only gate between request strings and
    filesystem paths, so it rejects '', '..', '\\', absolute paths, empty
    segments and trailing dots by construction — plus the Win32 device
    names, which no platform can store as a file."""
    if not isinstance(slug, str) or not slug:
        raise SchemaError(str(slug), "slug must be a non-empty string")
    for segment in slug.split("/"):
        if not SLUG_SEGMENT_RE.match(segment):
            raise SchemaError(
                slug,
                f"invalid slug segment {segment!r} — use lowercase letters, digits, "
                "'.', '_' or '-', and '/' to separate folders (no trailing '.')",
            )
        if segment.split(".", 1)[0] in _WIN_DEVICE_NAMES:
            raise SchemaError(
                slug,
                f"slug segment {segment!r} is a reserved Windows device name — pick another name",
            )
    return slug


# str(path) -> (mtime_ns, parsed object); module-level so nodes/tests share
# it. Keyed by PATH ALONE with the mtime in the value: keying by
# (path, mtime) instead would leave one dead parsed object behind per file
# edit, and nothing ever evicts them in a long-running ComfyUI session.
_PARSE_CACHE: dict = {}


def forget_parsed(path):
    """Evict one file's parse-cache generation. The cache above validates by
    mtime alone, so two writes to the same file inside a single filesystem
    tick (~1 ms on this NTFS host, coarser on some Linux filesystems) would
    serve the FIRST parse for the second write. Every writer in this pack
    knows the exact path it just touched, so eviction is exact rather than a
    heuristic — and the shared cache keeps its value for everyone else.
    Both the plain and the resolved spelling are dropped: writers resolve
    their target, `_scan` does not."""
    p = Path(path)
    _PARSE_CACHE.pop(str(p), None)
    with contextlib.suppress(OSError):  # unresolvable path: the plain key was all there was
        _PARSE_CACHE.pop(str(p.resolve()), None)


@dataclass(frozen=True)
class Entry:
    slug: str
    tier: str  # "factory" | "user"
    path: Path
    mtime_ns: int
    size: int


def merge_sections(factory, user):
    """Combined view of a same-slug section pair: user items merge into
    factory items by name (user version wins, new names append after),
    'hidden' tombstones a name out of the pools while staying visible to
    editors, and empty user fields inherit the factory value. Every item
    carries its origin tier so UIs can show where elements live."""
    items = {item.name: replace(item, origin="factory") for item in factory.items}
    for item in user.items:
        base = items.get(item.name)
        if item.hidden and base is not None and not item.text:
            items[item.name] = replace(base, hidden=True)  # bare tombstone keeps content visible
        else:
            items[item.name] = replace(item, origin="user")
    user_label = user.label if user.label != default_label(user.slug) else ""
    return Section(
        slug=factory.slug,
        label=user_label or factory.label,
        items=tuple(items.values()),
        description=user.description or factory.description,
        negative=user.negative or factory.negative,
        tags=user.tags or factory.tags,
        excludes=user.excludes or factory.excludes,
        requires=user.requires or factory.requires,
        suits=user.suits or factory.suits,
        merged=True,
    )


class Library:
    def __init__(self, factory_root, user_root):
        self.factory_root = Path(factory_root)
        self.user_root = Path(user_root) if user_root else None
        # Per-instance directory-scan memo. Library objects are created per
        # request / per node execution, so one instance sees one consistent
        # snapshot; without this, listing endpoints walk the whole tree once
        # PER SLUG (quadratic — measured ~14s for the composer's first open).
        self._scan_cache: dict = {}

    def invalidate(self):
        """Drop the scan memo after any write so this instance sees it."""
        self._scan_cache.clear()

    def ensure_user_dirs(self):
        if self.user_root:
            for kind in KINDS:
                (self.user_root / kind).mkdir(parents=True, exist_ok=True)

    # -- discovery ---------------------------------------------------------

    def _scan(self, kind):
        """slug -> Entry, user tier overriding factory. Memoized per
        instance; writers call invalidate()."""
        cached = self._scan_cache.get(kind)
        if cached is not None:
            return cached
        entries = {}
        for tier, root in (("factory", self.factory_root), ("user", self.user_root)):
            if not root:
                continue
            kind_dir = root / kind
            if not kind_dir.is_dir():
                continue
            for path in sorted(kind_dir.rglob("*.json")):
                slug = path.relative_to(kind_dir).with_suffix("").as_posix()
                try:
                    stat = path.stat()
                except OSError:
                    # Deleted (or made unreadable) between rglob and stat — a
                    # concurrent Composer delete must not take down every
                    # listing. A file that vanished is one we never scanned.
                    continue
                entries[slug] = Entry(slug, tier, path, stat.st_mtime_ns, stat.st_size)
        self._scan_cache[kind] = entries
        return entries

    def pack_profiles(self):
        """Pack-level target-model profiles: <root>/profiles.json, factory
        overlaid by the user tier, per profile name. Memoized with the scan
        cache; malformed files are skipped with a warning."""
        cached = self._scan_cache.get("@profiles")
        if cached is not None:
            return cached
        from .profiles import overlay_profile

        merged = {}
        for root in (self.factory_root, self.user_root):
            if not root:
                continue
            path = Path(root) / "profiles.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                _log.warning("ignoring malformed %s: %s", path, exc)
                continue
            if not isinstance(data, dict):
                _log.warning("ignoring malformed %s: root must be a JSON object", path)
                continue
            profiles = data.get("profiles") or {}
            if not isinstance(profiles, dict):
                _log.warning("ignoring malformed %s: 'profiles' must be an object", path)
                continue
            for name, profile in profiles.items():
                if str(name) == "standard":  # reserved: the unprofiled render
                    _log.warning("ignoring reserved profile name 'standard' in %s", path)
                    continue
                if not isinstance(profile, dict):
                    _log.warning("ignoring malformed profile %r in %s", name, path)
                    continue
                merged[str(name)] = overlay_profile(merged.get(str(name), {}), profile)
        self._scan_cache["@profiles"] = merged
        return merged

    def section_slugs(self):
        return sorted(self._scan("sections"))

    def section_folders(self):
        folders = set()
        for slug in self._scan("sections"):
            parts = slug.split("/")
            for i in range(1, len(parts)):
                folders.add("/".join(parts[:i]))
        return sorted(folders)

    def template_slugs(self):
        return sorted(self._scan("templates"))

    def tier_of(self, kind, slug):
        entry = self._scan(kind).get(slug)
        return entry.tier if entry else ""

    def tiers_of(self, kind, slug):
        """Every tier that has a FILE for this slug, factory first.

        _scan keeps only the winner, so it cannot answer "a factory version
        also exists" — and that is exactly what a user needs to know before
        deciding whether their file is an improvement or a mistake. Asked of
        the filesystem directly: two stats, no cache to invalidate.
        """
        found = []
        for tier, root in (("factory", self.factory_root), ("user", self.user_root)):
            if root and (Path(root) / kind / f"{slug}.json").is_file():
                found.append(tier)
        return tuple(found)

    # -- aliases -----------------------------------------------------------

    def _aliases(self, kind):
        """Merged old-slug -> new-slug map from <tier>/aliases.json (user
        entries override factory). A malformed alias file is skipped with a
        warning — the compatibility layer must never become a new way to
        fail. Only read on a lookup MISS, so the extra file I/O is rare."""
        merged = {}
        for root in (self.factory_root, self.user_root):
            if not root:
                continue
            path = Path(root) / "aliases.json"
            if not path.is_file():
                continue
            try:
                table = json.loads(path.read_text(encoding="utf-8")).get(kind) or {}
                merged.update({str(k): str(v) for k, v in table.items()})
            except Exception as exc:
                _log.warning("ignoring malformed %s: %s", path, exc)
        return merged

    def _alias_target(self, kind, slug, exists):
        """Follow the alias chain from `slug` until `exists(candidate)` is
        true. Returns the first existing candidate or None; cycles and dead
        chains end as None instead of looping."""
        aliases = self._aliases(kind)
        current, seen = slug, {slug}
        while current in aliases:
            current = aliases[current]
            if exists(current):
                return current
            if current in seen:
                return None
            seen.add(current)
        return None

    # -- loading -----------------------------------------------------------

    def _parse_file(self, path, slug, parser):
        try:
            stat = path.stat()
        except OSError as exc:
            raise SchemaError(str(path), f"cannot read file: {exc}") from exc
        key = str(path)
        cached = _PARSE_CACHE.get(key)
        if cached is not None and cached[0] == stat.st_mtime_ns:
            return cached[1]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SchemaError(str(path), f"invalid JSON: {exc}") from exc
        except OSError as exc:
            raise SchemaError(str(path), f"cannot read file: {exc}") from exc
        parsed = parser(data, slug, str(path))
        _PARSE_CACHE[key] = (stat.st_mtime_ns, parsed)  # replaces the old generation
        return parsed

    def _load(self, kind, slug, parser, not_found):
        entries = self._scan(kind)
        entry = entries.get(slug)
        if entry is None:
            target = self._alias_target(kind, slug, lambda s: s in entries)
            if target is not None:
                return self._load(kind, target, parser, not_found)
            raise not_found(slug, list(entries))
        return self._parse_file(entry.path, slug, parser)

    def load_section(self, slug, tier=None):
        """Sections COMPOUND across tiers: a user file over a factory slug
        extends it by default (items merge by name, user wins; 'hidden'
        tombstones a name; section fields inherit when empty). A user file
        with '"replaces": true' shadows the factory file entirely —
        templates always replace, only sections merge.

        `tier` returns ONE tier's file unmerged, which is the only way to see
        what the factory shipped under a slug your file extends."""
        if tier:
            path = self._tier_path("sections", slug, tier)
            if path is None:
                raise SectionNotFoundError(slug, list(self._scan("sections")))
            return self._parse_file(path, slug, parse_section)
        entries = self._scan("sections")
        entry = entries.get(slug)
        if entry is None:
            target = self._alias_target("sections", slug, lambda s: s in entries)
            if target is not None:
                return self.load_section(target)
            raise SectionNotFoundError(slug, list(entries))
        section = self._parse_file(entry.path, slug, parse_section)
        if entry.tier != "user" or section.replaces:
            return section
        factory_slug = slug
        factory_path = self.factory_root / "sections" / f"{slug}.json"
        if not factory_path.is_file():
            # A factory rename must not detach a user extend-file: follow
            # the alias table to the renamed factory baseline before giving
            # up on merging.
            def _factory_file(s):
                return (self.factory_root / "sections" / f"{s}.json").is_file()

            target = self._alias_target("sections", slug, _factory_file)
            if target is None:
                return section
            factory_slug = target
            factory_path = self.factory_root / "sections" / f"{target}.json"
        factory = self._parse_file(factory_path, factory_slug, parse_section)
        return merge_sections(factory, section)

    def load_template(self, slug, tier=None):
        """The winning template, or one specific tier's file.

        `tier` is how a UI shows the factory version of a slug a user file has
        shadowed — a comparison, not a mode: nothing about loading it changes
        which file wins for a render.
        """
        if tier:
            path = self._tier_path("templates", slug, tier)
            if path is None:
                raise TemplateNotFoundError(slug, list(self._scan("templates")))
            return self._parse_file(path, slug, parse_template)
        return self._load("templates", slug, parse_template, TemplateNotFoundError)

    def _tier_path(self, kind, slug, tier):
        root = self.factory_root if tier == "factory" else self.user_root
        if not root:
            return None
        path = Path(root) / kind / f"{slug}.json"
        return path if path.is_file() else None

    def scope_items(self, ref):
        """Items in scope of `ref` (leaf slug or folder), as
        (qualified_name, section, item) with qualified_name relative to ref.
        Leaf: qualified = item name. Folder: '<subpath>/<item name>',
        sections sorted by slug, items in file order."""
        ref = ref.strip("/")
        slugs = self._scan("sections")
        matching = sorted(s for s in slugs if s.startswith(ref + "/"))
        if ref in slugs:
            # Leaf wins (flipping that would silently re-point live slots),
            # but it must not be silent: a same-named folder underneath is
            # unreachable through this ref, so say so once per process.
            if matching and ref not in _SHADOW_WARNED:
                _SHADOW_WARNED.add(ref)
                _log.warning(
                    "section '%s' is both a leaf file and a folder; the leaf wins and "
                    "%s stay unreachable through this ref — rename one of them",
                    ref,
                    ", ".join(f"'{s}'" for s in matching),
                )
            section = self.load_section(ref)
            return [(item.name, section, item) for item in section.items if not item.hidden]
        if not matching:
            target = self._alias_target(
                "sections",
                ref,
                lambda s: s in slugs or any(x.startswith(s + "/") for x in slugs),
            )
            if target is not None:
                return self.scope_items(target)
            raise SectionNotFoundError(ref, list(slugs) + self.section_folders())
        result = []
        for slug in matching:
            section = self.load_section(slug)
            sub = slug[len(ref) + 1 :]
            result.extend(
                (f"{sub}/{item.name}", section, item) for item in section.items if not item.hidden
            )
        return result

    # -- user-tier writes --------------------------------------------------

    def save_user(self, kind, slug, data):
        """Validate `data` with the real parser, then write it VERBATIM as a
        user-tier file (dumping would strip unknown keys the schema
        tolerates). Returns the written path. Factory is never written."""
        if kind not in WRITABLE_KINDS:
            raise SchemaError(kind, f"unwritable kind (writable: {', '.join(WRITABLE_KINDS)})")
        if self.user_root is None:
            raise SchemaError(slug, "no user library directory is configured")
        validate_slug(slug)
        if not isinstance(data, dict):
            raise SchemaError(slug, "file content must be a JSON object")
        parser = parse_section if kind == "sections" else parse_template
        parser(data, slug, f"user:{slug}")  # never write a file the engine can't read
        kind_dir = (self.user_root / kind).resolve()
        target = (kind_dir / f"{slug}.json").resolve()
        if kind_dir not in target.parents:  # defense in depth after validate_slug
            raise SchemaError(slug, "slug escapes the user library directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, target)
        # The UNRESOLVED spelling is what `_scan` caches under; forget_parsed
        # drops the resolved one too.
        forget_parsed(self.user_root / kind / f"{slug}.json")
        self.invalidate()
        return target

    def delete_user(self, kind, slug):
        """Delete a user-tier file (factory is read-only). Returns True when
        a factory file with the same slug remains — the slug reverts to
        factory content instead of disappearing."""
        if kind not in WRITABLE_KINDS:
            raise SchemaError(kind, f"unwritable kind (writable: {', '.join(WRITABLE_KINDS)})")
        not_found = SectionNotFoundError if kind == "sections" else TemplateNotFoundError
        validate_slug(slug)
        path = self.user_root / kind / f"{slug}.json" if self.user_root else None
        if path is None or not path.is_file():
            user_slugs = [s for s, e in self._scan(kind).items() if e.tier == "user"]
            raise not_found(slug, user_slugs)
        path.unlink()
        forget_parsed(path)
        self.invalidate()
        entry = self._scan(kind).get(slug)
        return entry is not None and entry.tier == "factory"

    # -- change detection --------------------------------------------------

    def fingerprint(self):
        # Change detection must always reflect the disk, never the memo.
        self.invalidate()
        lines = []
        for kind in ("sections", "templates"):
            for entry in self._scan(kind).values():
                lines.append(f"{kind}|{entry.tier}|{entry.slug}|{entry.mtime_ns}|{entry.size}")
        # root-level config files change rendering too (profiles, aliases)
        for tier, root in (("factory", self.factory_root), ("user", self.user_root)):
            if not root:
                continue
            for fname in ("profiles.json", "aliases.json"):
                path = Path(root) / fname
                if path.is_file():
                    stat = path.stat()
                    lines.append(f"root|{tier}|{fname}|{stat.st_mtime_ns}|{stat.st_size}")
        return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()


def default_roots():
    """(factory_root, user_root). User root: $MRLN_PROMPT_DIR, else the
    ComfyUI user directory, else a gitignored dir inside the pack."""
    factory = Path(__file__).resolve().parents[1] / "data" / "prompt"
    env = os.environ.get("MRLN_PROMPT_DIR")
    if env:
        return factory, Path(env)
    try:
        import folder_paths  # ComfyUI runtime only

        return factory, Path(folder_paths.get_user_directory()) / "mrln" / "prompt"
    except Exception:
        return factory, Path(__file__).resolve().parents[2] / "user_data" / "prompt"


def open_library():
    return Library(*default_roots())
