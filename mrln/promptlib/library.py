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
from .schema import parse_section, parse_template

KINDS = ("sections", "templates", "profiles", "system_prompts")

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
