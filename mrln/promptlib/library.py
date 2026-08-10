"""Two-tier library: factory content shipped with the pack + persistent user
tier. User file with the same slug REPLACES the factory file entirely.
Slug = file path relative to the kind dir, POSIX separators, no extension.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import SchemaError, SectionNotFoundError, TemplateNotFoundError
from .schema import SLUG_SEGMENT_RE, parse_section, parse_template

KINDS = ("sections", "templates", "profiles", "system_prompts")
WRITABLE_KINDS = ("sections", "templates")


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

    # -- loading -----------------------------------------------------------

    def _load(self, kind, slug, parser, not_found):
        entries = self._scan(kind)
        entry = entries.get(slug)
        if entry is None:
            raise not_found(slug, list(entries))
        key = (str(entry.path), entry.mtime_ns)
        if key in _PARSE_CACHE:
            return _PARSE_CACHE[key]
        try:
            with open(entry.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SchemaError(str(entry.path), f"invalid JSON: {exc}") from exc
        except OSError as exc:
            raise SchemaError(str(entry.path), f"cannot read file: {exc}") from exc
        parsed = parser(data, slug, str(entry.path))
        _PARSE_CACHE[key] = parsed
        return parsed

    def load_section(self, slug):
        return self._load("sections", slug, parse_section, SectionNotFoundError)

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
            return [(item.name, section, item) for item in section.items]
        matching = sorted(s for s in slugs if s.startswith(ref + "/"))
        if not matching:
            raise SectionNotFoundError(ref, list(slugs) + self.section_folders())
        result = []
        for slug in matching:
            section = self.load_section(slug)
            sub = slug[len(ref) + 1 :]
            result.extend((f"{sub}/{item.name}", section, item) for item in section.items)
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
