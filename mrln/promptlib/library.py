"""Two-tier library: factory content shipped with the pack + persistent user
tier. Slug = file path relative to the kind dir, POSIX separators, no
extension. Same-slug SECTIONS compound (the user file extends factory unless
it sets '"replaces": true'); same-slug templates replace factory entirely.

Renamed slugs never just die: `aliases.json` at each tier root maps old slug
-> new slug, consulted only when a lookup misses. A factory restructure ships
aliases for every renamed slug so existing user templates keep loading.
"""

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


def validate_slug(slug):
    """Path-safe slug or SchemaError. Every '/'-segment must match
    SLUG_SEGMENT_RE — this is the only gate between request strings and
    filesystem paths, so it rejects '', '..', '\\', absolute paths and
    empty segments by construction."""
    if not isinstance(slug, str) or not slug:
        raise SchemaError(str(slug), "slug must be a non-empty string")
    for segment in slug.split("/"):
        if not SLUG_SEGMENT_RE.match(segment):
            raise SchemaError(
                slug,
                f"invalid slug segment {segment!r} — use lowercase letters, digits, "
                "'.', '_' or '-', and '/' to separate folders",
            )
    return slug


# (path, mtime_ns) -> parsed object; module-level so nodes/tests share it
_PARSE_CACHE: dict = {}


@dataclass(frozen=True)
class Entry:
    slug: str
    kind: str
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

    def ensure_user_dirs(self):
        if self.user_root:
            for kind in KINDS:
                (self.user_root / kind).mkdir(parents=True, exist_ok=True)

    # -- discovery ---------------------------------------------------------

    def _scan(self, kind):
        """slug -> Entry, user tier overriding factory."""
        entries = {}
        for tier, root in (("factory", self.factory_root), ("user", self.user_root)):
            if not root:
                continue
            kind_dir = root / kind
            if not kind_dir.is_dir():
                continue
            for path in sorted(kind_dir.rglob("*.json")):
                slug = path.relative_to(kind_dir).with_suffix("").as_posix()
                stat = path.stat()
                entries[slug] = Entry(slug, kind, tier, path, stat.st_mtime_ns, stat.st_size)
        return entries

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
        key = (str(path), stat.st_mtime_ns)
        if key in _PARSE_CACHE:
            return _PARSE_CACHE[key]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SchemaError(str(path), f"invalid JSON: {exc}") from exc
        except OSError as exc:
            raise SchemaError(str(path), f"cannot read file: {exc}") from exc
        parsed = parser(data, slug, str(path))
        _PARSE_CACHE[key] = parsed
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

    def load_section(self, slug):
        """Sections COMPOUND across tiers: a user file over a factory slug
        extends it by default (items merge by name, user wins; 'hidden'
        tombstones a name; section fields inherit when empty). A user file
        with '"replaces": true' shadows the factory file entirely —
        templates always replace, only sections merge."""
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
        factory_path = self.factory_root / "sections" / f"{slug}.json"
        if not factory_path.is_file():
            return section
        factory = self._parse_file(factory_path, slug, parse_section)
        return merge_sections(factory, section)

    def load_template(self, slug):
        return self._load("templates", slug, parse_template, TemplateNotFoundError)

    def scope_items(self, ref):
        """Items in scope of `ref` (leaf slug or folder), as
        (qualified_name, section, item) with qualified_name relative to ref.
        Leaf: qualified = item name. Folder: '<subpath>/<item name>',
        sections sorted by slug, items in file order."""
        ref = ref.strip("/")
        slugs = self._scan("sections")
        if ref in slugs:
            section = self.load_section(ref)
            return [(item.name, section, item) for item in section.items if not item.hidden]
        matching = sorted(s for s in slugs if s.startswith(ref + "/"))
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
        entry = self._scan(kind).get(slug)
        return entry is not None and entry.tier == "factory"

    # -- change detection --------------------------------------------------

    def fingerprint(self):
        lines = []
        for kind in ("sections", "templates"):
            for entry in self._scan(kind).values():
                lines.append(f"{kind}|{entry.tier}|{entry.slug}|{entry.mtime_ns}|{entry.size}")
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
