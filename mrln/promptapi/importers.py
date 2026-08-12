"""Migration on-ramps: bring content a user ALREADY owns into the library.

Two sources, because between them they hold most of the prompt content that
exists in ComfyUI installs today:

  wildcard folders   `.txt` / `.yaml` files (the dynamic-prompts ecosystem),
  and .zip archives  or a .zip of them, which is how every published pack is
                     actually distributed — Civitai's 'Wildcards' model type
                     ships nothing else
                     -> user sections under 'wildcards/…'
  A1111 styles.csv   name / prompt / negative rows
                     -> a user TEMPLATE per '{prompt}' row (prefix + suffix),
                        a user ITEM per plain row (section 'styles/a1111')

Both endpoints answer with the SAME plan shape `promptlib.import_bundle`
returns (written / skipped / missing_factory / loras / dry_run, plus
needs_overwrite when a file is in the way), so the Composer's existing
import-plan card renders them with no new UI work. The extra `warnings` key
is a list of ready-to-print strings — a client that does not know about it
loses nothing.

Rules this module inherits from the bundle importer and does not bend:

- Validate EVERYTHING with the real parsers first, write afterwards. A
  half-imported folder is worse than a refused one, so a single unparseable
  draft refuses the whole import.
- Every write goes through `Library.save_user` (slug validation + user tier).
  The factory tier is never touched, and no string from the source files
  reaches the filesystem any other way.
- The content lints that guard FACTORY content (adults-only, artist names,
  text_short) deliberately do NOT apply here: this is the user's own
  third-party content landing in their own tier.

Third-party text is imported VERBATIM, and the plan says what that means:
`{a|b}` happens to be this engine's own inline-choice syntax and really does
work, while `__name__` does not resolve at all. Those two are reported as
separate warnings on purpose — a false reassurance would be worse than no
warning. Braces that are neither (a bare `{name}` variable, an unbalanced
brace) are reported as a third, louder class, because the engine expands item
text and template prefix/suffix at render time.

Reading a caller-named folder off disk is the one dangerous thing here, so
the walk is capped in every direction (see MAX_* below) and a filesystem root
is refused outright: a mis-pasted 'C:\\' fails fast instead of walking a disk.
"""

import csv
import io
import json
import os
import re
from pathlib import Path

from .. import promptlib as pl
from ..promptlib import textexpr
from ..promptlib.schema import is_reserved_name, slugify
from .core import ApiError, _guarded

# -- limits -------------------------------------------------------------------
# Deliberately small enough that a pointed-at system directory trips one of
# them within a second, and large enough for every real wildcard pack: the
# biggest published collections are a few hundred files of a few KB.
MAX_WALK_DEPTH = 8  # folder levels below the import root
MAX_DIR_ENTRIES = 20_000  # directory entries visited in total
MAX_WILDCARD_FILES = 2_000  # matching .txt/.yaml files
MAX_WILDCARD_BYTES = 16 * 1024 * 1024  # summed size of those files
MAX_FILE_BYTES = 4 * 1024 * 1024  # single file (skipped with a warning)
MAX_SECTION_ITEMS = 5_000  # items in one imported section
MAX_TOTAL_ITEMS = 50_000  # items in one import
MAX_CSV_BYTES = 8 * 1024 * 1024  # styles.csv
MAX_STYLE_ROWS = 5_000
MAX_WARNINGS = 200  # then one "… and N more" line

WILDCARD_PREFIX = "wildcards"
STYLES_PREFIX = "styles"
STYLES_SECTION = "styles/a1111"
WILDCARD_SUFFIXES = (".txt", ".yaml", ".yml")

PATH_REMEDIATION = (
    "pass the absolute path of the folder that holds your wildcard files "
    "(for example the 'wildcards' folder of sd-dynamic-prompts)"
)
CSV_PATH_REMEDIATION = (
    "pass the absolute path of an A1111 'styles.csv' file "
    "(the one next to webui.py, or an exported copy of it)"
)
YAML_REMEDIATION = (
    "PyYAML ships with ComfyUI — install it into this Python environment "
    "('pip install PyYAML'), or point the import at a folder of .txt files"
)
LIMIT_REMEDIATION = (
    "point the import at the wildcard folder itself instead of a parent that "
    "contains it, or split the collection and import it in parts"
)

_BOM_UTF8 = b"\xef\xbb\xbf"
# Weighted line syntax the dynamic-prompts ecosystem uses: "3::rare option".
_WEIGHT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*::\s*(.*)$", re.DOTALL)
# A nested wildcard reference. Names may carry a path ('__hair/color__') and
# the ecosystem's glob forms ('__cars/*__'), so the charset is generous.
_NESTED_RE = re.compile(r"__([A-Za-z0-9][A-Za-z0-9_./*+-]*)__")
_PROMPT_PLACEHOLDER_RE = re.compile(r"\{prompt\}", re.IGNORECASE)
_SLUG_BAD_RE = re.compile(r"[^a-z0-9._-]+")


class ImporterError(ApiError):
    """A refused import. Carries its own status and remediation so each
    refusal says the actionable thing (a wrong path, a missing PyYAML and a
    tripped walk limit want three different next steps) instead of the
    guard's generic 400 text."""

    def __init__(self, message, remediation, status=400):
        super().__init__(message)
        self.remediation = remediation
        self.status = status

    def body(self):
        return {"error": str(self), "remediation": self.remediation}


# -- source paths -------------------------------------------------------------


def resolve_source(raw, *, kind="folder"):
    """The caller's `path` as an absolute Path, or ImporterError.

    Refuses anything that is not the requested kind, and refuses a filesystem
    root outright: walking a whole drive is never what the user meant, and
    failing on the first call is much kinder than a walk that "works"."""
    if not isinstance(raw, str) or not raw.strip():
        raise ImporterError(
            "missing required parameter 'path'",
            PATH_REMEDIATION if kind == "folder" else CSV_PATH_REMEDIATION,
        )
    text = os.path.expandvars(raw.strip().strip('"').strip("'"))
    remediation = PATH_REMEDIATION if kind == "folder" else CSV_PATH_REMEDIATION
    try:
        path = Path(text).expanduser().resolve()
        exists = path.exists()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ImporterError(f"'{raw}' is not a usable path: {exc}", remediation) from None
    if not exists:
        raise ImporterError(f"nothing exists at '{path}'", remediation, 404)
    if kind == "folder":
        if not path.is_dir():
            raise ImporterError(f"'{path}' is a file, not a folder", remediation)
        if path.parent == path:
            raise ImporterError(
                f"'{path}' is a filesystem root — importing a whole drive is refused",
                LIMIT_REMEDIATION,
            )
    elif not path.is_file():
        raise ImporterError(f"'{path}' is a folder, not a file", remediation)
    return path


def _rel(root, path):
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return Path(path).name


def scan_wildcard_folder(root):
    """([(path, size)], notes) for every .txt/.yaml/.yml file under `root`.

    Iterative, capped in four directions, and it never follows a symlink or
    junction into a folder — a link loop would otherwise walk forever. Files
    come back sorted so the same folder always plans the same way."""
    files, notes = [], []
    total = 0
    seen = 0
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as scan:
                entries = sorted(scan, key=lambda e: e.name.lower())
        except OSError as exc:
            notes.append(
                f"could not read folder '{_rel(root, current)}': "
                f"{exc.strerror or exc} — nothing from it was imported"
            )
            continue
        for entry in entries:
            seen += 1
            if seen > MAX_DIR_ENTRIES:
                raise ImporterError(
                    f"the folder tree under '{root}' holds more than {MAX_DIR_ENTRIES} "
                    "entries — refusing to keep walking",
                    LIMIT_REMEDIATION,
                )
            linked = entry.is_symlink()
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if linked and not is_dir:
                try:
                    is_dir = entry.is_dir()  # follows the link, only to classify it
                except OSError:
                    is_dir = False
                if is_dir:
                    notes.append(
                        f"skipped linked folder '{_rel(root, entry.path)}' — symlinks and "
                        "junctions are never followed (a link loop would walk forever)"
                    )
                    continue
            if is_dir:
                if depth + 1 > MAX_WALK_DEPTH:
                    notes.append(
                        f"skipped '{_rel(root, entry.path)}' and everything under it — "
                        f"deeper than the {MAX_WALK_DEPTH}-level import limit"
                    )
                    continue
                stack.append((Path(entry.path), depth + 1))
                continue
            if Path(entry.name).suffix.lower() not in WILDCARD_SUFFIXES:
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                notes.append(
                    f"skipped '{_rel(root, entry.path)}': {size // 1024} KiB is over the "
                    f"{MAX_FILE_BYTES // 1024} KiB per-file limit for a wildcard file"
                )
                continue
            files.append((Path(entry.path), size))
            total += size
            if len(files) > MAX_WILDCARD_FILES:
                raise ImporterError(
                    f"more than {MAX_WILDCARD_FILES} wildcard files under '{root}'",
                    LIMIT_REMEDIATION,
                )
            if total > MAX_WILDCARD_BYTES:
                raise ImporterError(
                    f"the wildcard files under '{root}' add up to more than "
                    f"{MAX_WILDCARD_BYTES // (1024 * 1024)} MiB",
                    LIMIT_REMEDIATION,
                )
    files.sort(key=lambda pair: pair[0].as_posix().lower())
    return files, notes


# -- text: decoding, lines, syntax classification -----------------------------


def decode_text(raw):
    """(text, note) for bytes off a stranger's disk. UTF-8 with or without a
    BOM covers almost everything; UTF-16 shows up in spreadsheet exports and
    Windows-1252 in older hand-edited files, and a fallback decode is
    reported because it CAN mangle characters."""
    if raw.startswith(_BOM_UTF8):
        raw = raw[len(_BOM_UTF8) :]
    elif raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "the file is UTF-16 (a spreadsheet export)"
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return (
            raw.decode("cp1252", errors="replace"),
            "the file is not valid UTF-8 — it was read as Windows-1252, so check "
            "accented characters in the imported text",
        )


def parse_wildcard_line(line):
    """(text, weight) for one wildcard-file line, or None when it carries no
    item: blank lines and full-line '#' comments are skipped, and the
    ecosystem's 'N::text' weight prefix is honored. An inline '#' is NOT a
    comment — real wildcard files contain '#1 ranked' style text."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    match = _WEIGHT_RE.match(text)
    if match is not None:
        rest = match.group(2).strip()
        if not rest:
            return None  # "3::" weights nothing
        return rest, float(match.group(1))
    return text, 1.0


def _walk_nodes(nodes, found):
    """Classify a parsed inline expression. Duck-typed on textexpr.parse()'s
    public return value (a group carries `alts`, a variable carries `name`) so
    no private class name is imported."""
    for node in nodes:
        if isinstance(node, str):
            continue
        alts = getattr(node, "alts", None)
        if alts is not None:
            found["choice"] = True
            for alt in alts:
                _walk_nodes(alt, found)
        else:
            found["vars"].add(str(getattr(node, "name", "?")))


def analyze_text(text):
    """(nested_refs, has_inline_choice, variable_names, syntax_error) for one
    piece of imported text — everything the plan needs to say, precisely, what
    this engine will and will not do with it at render time."""
    text = str(text or "")
    refs = tuple(dict.fromkeys(_NESTED_RE.findall(text)))
    if "{" not in text and "}" not in text:
        return refs, False, frozenset(), ""
    try:
        nodes = textexpr.parse(text)
    except pl.PromptLibError as exc:
        return refs, False, frozenset(), str(exc)
    found = {"choice": False, "vars": set()}
    _walk_nodes(nodes, found)
    return refs, found["choice"], frozenset(found["vars"]), ""


def _sample(names, limit=6):
    names = list(names)
    shown = ", ".join(str(n) for n in names[:limit])
    if len(names) > limit:
        shown += f", … (+{len(names) - limit})"
    return shown


def _excerpt(text, width=60):
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def syntax_warnings(where, texts):
    """The verbatim-import warnings for a group of imported strings.

    Four classes, kept apart on purpose:
      __name__      does NOT resolve — renders literally
      {a|b}         DOES work — it is this engine's own inline choice syntax
      {name}        a variable: renders only if the template defines it
      broken braces the item cannot render at all until it is edited
    """
    refs, variables, broken = {}, {}, []
    choices = 0
    for text in texts:
        found_refs, choice, found_vars, error = analyze_text(text)
        for ref in found_refs:
            refs.setdefault(ref, 0)
            refs[ref] += 1
        for name in sorted(found_vars):
            variables.setdefault(name, 0)
            variables[name] += 1
        if choice:
            choices += 1
        if error:
            broken.append(text)
    out = []
    if refs:
        out.append(
            f"{where}: {sum(refs.values())} line(s) reference another wildcard file "
            f"(__{'__, __'.join(list(refs)[:6])}__) and were imported verbatim — MRLN does "
            "NOT resolve __name__, so those items render the reference as literal text; "
            "repoint them at the matching 'wildcards/…' section by hand"
        )
    if choices:
        out.append(
            f"{where}: {choices} line(s) use inline {{a|b}} choices — that IS MRLN's own "
            "wildcard syntax, so they work exactly as imported (one alternative is drawn "
            "per render)"
        )
    if variables:
        out.append(
            f"{where}: {len(variables)} brace placeholder(s) without alternatives "
            f"({_sample('{' + n + '}' for n in variables)}) — MRLN reads {{name}} as a "
            "variable, so an item using one only renders when the template defines that "
            "variable or a slot with that id; otherwise the render fails"
        )
    if broken:
        out.append(
            f"{where}: {len(broken)} line(s) carry braces MRLN cannot parse (unbalanced, or "
            "a literal brace that needs doubling) and were imported verbatim — those items "
            f"fail to render until edited, starting with '{_excerpt(broken[0])}'"
        )
    return out


# -- slugs --------------------------------------------------------------------


def slug_segment(raw):
    """One filename/key turned into a slug segment: lowercased, anything
    outside the slug charset collapsed to '-', and leading/trailing
    punctuation trimmed. May return '' — the caller reports that instead of
    inventing a name."""
    segment = _SLUG_BAD_RE.sub("-", str(raw).strip().lower())
    segment = re.sub(r"-{2,}", "-", segment)
    while segment and not segment[0].isalnum():
        segment = segment[1:]
    while segment and segment[-1] in ".-":
        segment = segment[:-1]
    return segment


def derive_slug(prefix, parts):
    """'<prefix>/<slugified parts>' validated by the library's own gate.

    Raises SchemaError for anything the gate refuses — an empty segment, a
    trailing dot, a Win32 device name ('con', 'nul', …). A wildcard folder off
    the internet is exactly where those turn up, and a warning naming the file
    beats silently writing a mangled neighbour of the name the user asked for.
    """
    segments = [slug_segment(part) for part in parts]
    for raw, segment in zip(parts, segments, strict=True):
        if not segment:
            raise pl.SchemaError(
                str(raw), "nothing usable is left of this name after slugification"
            )
    return pl.validate_slug("/".join([prefix, *segments]))


# -- section / template drafts ------------------------------------------------


def _items_from_entries(entries, where, warnings):
    """[(text, weight)] -> section item dicts. Item names come from the text
    (the way a hand-written section names its items), deduplicated, and never
    a reserved selection token — an item called 'random' could never be
    picked, and the schema refuses it outright."""
    items, used = [], set()
    if len(entries) > MAX_SECTION_ITEMS:
        warnings.append(
            f"{where}: {len(entries)} lines is over the {MAX_SECTION_ITEMS}-item limit "
            "for one section — only the first were imported"
        )
        entries = entries[:MAX_SECTION_ITEMS]
    for text, weight in entries:
        base = slugify(text) or "item"
        if is_reserved_name(base):
            base = f"{base}-item"
        name, counter = base, 2
        while name in used:
            name = f"{base}-{counter}"
            counter += 1
        used.add(name)
        item = {"name": name, "text": text}
        if weight != 1.0:
            item["weight"] = weight
        items.append(item)
    return items


def _section_draft(slug, label, source, entries, where, warnings):
    data = {
        "label": label,
        "description": f"imported from wildcard file '{source}'",
        "items": _items_from_entries(entries, where, warnings),
    }
    warnings.extend(syntax_warnings(where, [item["text"] for item in data["items"]]))
    return data


def _yaml_groups(yaml, path, source, dir_parts, warnings):
    """[(slug parts, [(text, weight)])] for one YAML wildcard file.

    NESTING RULE: a mapping key becomes a slug segment, a list becomes the
    section's items, and a scalar becomes a one-item section. The FILE STEM is
    deliberately not part of the slug — the ecosystem's YAML files use their
    top-level keys as the wildcard namespace, so 'clothing: {top: [...]}' in
    any file lands on 'wildcards/clothing/top', which is exactly where
    'clothing/top.txt' lands too. Both mirror the '__clothing/top__' the
    user's own prompts already say. The file's FOLDER path stays in front of
    the key path, so subfolders keep namespacing the way they do for .txt.
    """
    try:
        loaded = yaml.safe_load(path.read_bytes())
    except Exception as exc:  # yaml.YAMLError, plus anything a broken file throws
        warnings.append(f"skipped '{source}': it is not readable YAML ({_excerpt(exc, 120)})")
        return []
    if not isinstance(loaded, dict):
        warnings.append(
            f"skipped '{source}': a YAML wildcard file must map names to lists of "
            f"options at its top level (this one is {type(loaded).__name__})"
        )
        return []
    groups = []
    stack = [(list(dir_parts), loaded, 0)]
    while stack:
        parts, node, depth = stack.pop()
        for key in reversed(list(node)):
            value = node[key]
            child = [*parts, str(key)]
            if isinstance(value, dict):
                if depth + 1 > MAX_WALK_DEPTH:
                    warnings.append(
                        f"skipped '{source}' key '{'/'.join(child)}': deeper than the "
                        f"{MAX_WALK_DEPTH}-level import limit"
                    )
                    continue
                stack.append((child, value, depth + 1))
                continue
            if isinstance(value, (list, tuple)):
                raw_entries = list(value)
            elif isinstance(value, (str, int, float)):
                raw_entries = [value]
            else:
                warnings.append(
                    f"skipped '{source}' key '{'/'.join(child)}': a wildcard entry must be "
                    f"a list of options or a single option (this one is {type(value).__name__})"
                )
                continue
            entries, dropped = [], 0
            for raw in raw_entries:
                if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
                    parsed = parse_wildcard_line(str(raw))
                    if parsed is not None:
                        entries.append(parsed)
                else:
                    dropped += 1
            if dropped:
                warnings.append(
                    f"'{source}' key '{'/'.join(child)}': {dropped} entr(ies) are not plain "
                    "text (a nested list or mapping inside a list) and were skipped"
                )
            groups.append((child, entries))
    groups.sort(key=lambda pair: [str(p).lower() for p in pair[0]])
    return groups


def wildcard_drafts(root):
    """([(kind, slug, data)], warnings) for a whole wildcard folder.

    Nothing is written here: this builds every user-tier file the import would
    produce so the caller can validate the lot before touching the disk."""
    files, warnings = scan_wildcard_folder(root)
    drafts, claimed = [], {}
    yaml = None
    total_items = 0
    for path, _size in files:
        source = _rel(root, path)
        rel_parts = Path(source).parts
        if path.suffix.lower() == ".txt":
            try:
                raw = path.read_bytes()
            except OSError as exc:
                # vanished / locked between the scan and the read (AV, sync
                # client): one unreadable file must not refuse the folder
                warnings.append(f"skipped '{source}': cannot read it ({exc.strerror or exc})")
                continue
            text, note = decode_text(raw)
            if note:
                warnings.append(f"'{source}': {note}")
            entries = [
                parsed
                for parsed in (parse_wildcard_line(line) for line in text.splitlines())
                if parsed is not None
            ]
            groups = [(list(Path(source).with_suffix("").parts), entries)]
        else:
            if yaml is None:
                yaml = _load_yaml()
            groups = _yaml_groups(yaml, path, source, rel_parts[:-1], warnings)
        for parts, entries in groups:
            where = f"{source} → {'/'.join(str(p) for p in parts)}" if len(groups) > 1 else source
            if not entries:
                warnings.append(f"skipped '{where}': no usable lines (blank or all comments)")
                continue
            try:
                slug = derive_slug(WILDCARD_PREFIX, parts)
            except pl.SchemaError as exc:
                warnings.append(
                    f"skipped '{where}': {exc} — rename it and import again "
                    "(nothing was written under a guessed name)"
                )
                continue
            owner = claimed.get(slug)
            if owner is not None:
                warnings.append(
                    f"skipped '{where}': it maps onto section '{slug}', which "
                    f"'{owner}' already claimed — rename one of the two"
                )
                continue
            claimed[slug] = where
            label = str(parts[-1]).strip() or slug.rsplit("/", 1)[-1]
            data = _section_draft(slug, label, source, entries, where, warnings)
            total_items += len(data["items"])
            if total_items > MAX_TOTAL_ITEMS:
                raise ImporterError(
                    f"this folder would import more than {MAX_TOTAL_ITEMS} items",
                    LIMIT_REMEDIATION,
                )
            drafts.append(("sections", slug, data))
    return drafts, warnings


def _load_yaml():
    """PyYAML is Class B (ComfyUI guarantees it): soft-imported here, only
    when a .yaml file was actually found, and never a requirements entry."""
    try:
        import yaml
    except Exception:
        raise ImporterError(
            "this folder contains .yaml wildcard files, but PyYAML is not installed "
            "in this Python environment",
            YAML_REMEDIATION,
        ) from None
    return yaml


# -- styles.csv ---------------------------------------------------------------

_HEADER_ALIASES = {
    "name": "name",
    "style": "name",
    "title": "name",
    "prompt": "prompt",
    "positive": "prompt",
    "positive prompt": "prompt",
    "positive_prompt": "prompt",
    "negative": "negative",
    "negative prompt": "negative",
    "negative_prompt": "negative",
    "negativeprompt": "negative",
}


def sniff_delimiter(text):
    """The column separator of a real-world styles.csv. A1111 writes commas,
    but a European Excel round-trip writes semicolons and some exports use
    tabs, so csv.Sniffer decides and a header-count vote breaks its ties."""
    sample = text[:8192]
    cut = sample.rfind("\n")
    if cut > 0:
        sample = sample[:cut]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        head = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {sep: head.count(sep) for sep in (",", ";", "\t")}
        best = max(counts, key=lambda sep: counts[sep])
        return best if counts[best] else ","


def read_styles_file(path):
    """([row dicts], notes) from an A1111 styles.csv.

    The stdlib reader owns the messy parts real files are full of: quoted
    fields containing commas and newlines, doubled quotes, ragged rows. What
    this adds is the encoding sniff, the delimiter sniff, and a header map
    that refuses a file whose columns cannot be identified — importing
    garbage under invented column meanings would be worse than refusing."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ImporterError(
            f"cannot read '{path}': {exc.strerror or exc}",
            "check that the file is not open in another program and that this "
            "ComfyUI process may read it",
        ) from None
    if len(raw) > MAX_CSV_BYTES:
        raise ImporterError(
            f"'{path.name}' is {len(raw) // 1024} KiB, over the "
            f"{MAX_CSV_BYTES // 1024} KiB limit for a styles.csv",
            "split the file, or check that this really is a styles.csv",
        )
    text, note = decode_text(raw)
    notes = [f"'{path.name}': {note}"] if note else []
    if not text.strip():
        raise ImporterError(f"'{path.name}' is empty", CSV_PATH_REMEDIATION)
    delimiter = sniff_delimiter(text)
    if delimiter != ",":
        notes.append(
            f"'{path.name}' is separated by {'tabs' if delimiter == chr(9) else repr(delimiter)}, "
            "not commas — read accordingly"
        )
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    columns, rows = None, []
    for row in reader:
        if not any(str(cell).strip() for cell in row):
            continue  # blank row (trailing newlines, or a spacer the user left in)
        if columns is None:
            columns = {}
            for index, cell in enumerate(row):
                key = _HEADER_ALIASES.get(str(cell).strip().strip("\ufeff").lower())
                if key is not None and key not in columns:
                    columns[key] = index
            if "name" not in columns or "prompt" not in columns:
                raise ImporterError(
                    f"'{path.name}' has no recognizable header row — its first row reads "
                    f"{_excerpt(delimiter.join(row), 90)!r}, and this importer needs "
                    "'name' and 'prompt' columns (plus an optional negative column)",
                    "add a header row 'name,prompt,negative_prompt' (that is what A1111 "
                    "itself writes), or export the styles again",
                )
            continue
        if len(rows) >= MAX_STYLE_ROWS:
            notes.append(
                f"'{path.name}' has more than {MAX_STYLE_ROWS} rows — the rest were ignored"
            )
            break
        rows.append(
            {
                "line": reader.line_num,
                "name": _cell(row, columns.get("name")),
                "prompt": _cell(row, columns.get("prompt")),
                "negative": _cell(row, columns.get("negative")),
            }
        )
    if columns is None:
        raise ImporterError(f"'{path.name}' has no rows", CSV_PATH_REMEDIATION)
    if not rows:
        raise ImporterError(f"'{path.name}' has a header but no style rows", CSV_PATH_REMEDIATION)
    return rows, notes


def _cell(row, index):
    if index is None or index >= len(row):
        return ""
    return str(row[index]).strip()


def split_placeholder(prompt):
    """(prefix, suffix, count) around A1111's '{prompt}' marker. The first
    occurrence splits the style; a second one stays in the suffix verbatim
    (and is reported by the brace warnings, since this engine reads it as a
    variable)."""
    matches = list(_PROMPT_PLACEHOLDER_RE.finditer(prompt))
    if not matches:
        return prompt, "", 0
    first = matches[0]
    return prompt[: first.start()], prompt[first.end() :], len(matches)


def _tidy(fragment):
    """A style's half-sentence, trimmed of the separator it hung on. A1111
    writes 'masterpiece, {prompt}, detailed' — the prefix must not keep its
    trailing comma, or every render says 'masterpiece,, subject'."""
    return str(fragment or "").strip().strip(",").strip()


def styles_drafts(path):
    """([(kind, slug, data)], warnings) for one styles.csv. A '{prompt}' row
    becomes a template, every other row an item in one shared section."""
    rows, warnings = read_styles_file(path)
    drafts = []
    items, used_names = [], set()
    claimed, seen_names = {}, {}
    for row in rows:
        where = f"row at line {row['line']}"
        name = row["name"]
        if not name:
            warnings.append(f"skipped {where}: it has no style name")
            continue
        key = name.casefold()
        if key in seen_names:
            warnings.append(
                f"skipped {where} ('{_excerpt(name, 40)}'): a style with that name was "
                f"already imported from line {seen_names[key]} — the first one wins, so "
                "rename the later one if you need both"
            )
            continue
        seen_names[key] = row["line"]
        prompt, negative = row["prompt"], row["negative"]
        prefix, suffix, placeholders = split_placeholder(prompt)
        if placeholders:
            try:
                slug = derive_slug(STYLES_PREFIX, [name.replace("/", "-")])
            except pl.SchemaError as exc:
                warnings.append(
                    f"skipped {where} ('{_excerpt(name, 40)}'): {exc} — rename the style "
                    "and import again (nothing was written under a guessed name)"
                )
                continue
            owner = claimed.get(slug)
            if owner is not None:
                warnings.append(
                    f"skipped {where} ('{_excerpt(name, 40)}'): it maps onto template "
                    f"'{slug}', which '{owner}' already claimed — rename one of the two"
                )
                continue
            claimed[slug] = name
            data = {
                "label": name,
                "description": f"imported from A1111 styles.csv ('{path.name}')",
                "prefix": _tidy(prefix),
                "suffix": _tidy(suffix),
                "slots": [],
            }
            if negative:
                data["negative"] = negative
            if placeholders > 1:
                warnings.append(
                    f"{where} ('{_excerpt(name, 40)}'): the prompt carries {placeholders} "
                    "'{prompt}' markers — the first one split prefix from suffix, the rest "
                    "stayed in the text verbatim"
                )
            warnings.extend(syntax_warnings(f"template '{slug}'", [data["prefix"], data["suffix"]]))
            if _PROMPT_PLACEHOLDER_RE.search(negative):
                warnings.append(
                    f"template '{slug}': its negative keeps a '{{prompt}}' marker. MRLN does "
                    "not weave anything into a negative, so it renders as literal text — "
                    "edit the template negative"
                )
            drafts.append(("templates", slug, data))
            continue
        if not prompt:
            warnings.append(
                f"skipped {where} ('{_excerpt(name, 40)}'): its prompt column is empty. A "
                "negative-only style has no item form (an item needs text); add a '{prompt}' "
                "marker to the row to import it as a template instead"
            )
            continue
        base = slug_segment(name).replace("/", "-") or slugify(prompt) or "style"
        if is_reserved_name(base):
            base = f"{base}-item"
        item_name, counter = base, 2
        while item_name in used_names:
            item_name = f"{base}-{counter}"
            counter += 1
        used_names.add(item_name)
        item = {"name": item_name, "text": prompt}
        if negative:
            item["negative"] = negative
        items.append(item)
    if items:
        section = {
            "label": "A1111 Styles",
            "description": f"imported from A1111 styles.csv ('{path.name}')",
            "items": items,
        }
        warnings.extend(syntax_warnings(f"section '{STYLES_SECTION}'", [i["text"] for i in items]))
        drafts.append(("sections", STYLES_SECTION, section))
    if not drafts:
        raise ImporterError(
            f"'{path.name}' produced nothing importable — every row was skipped",
            "check the warnings for the reason per row; the file may use another dialect",
        )
    return drafts, warnings


# -- the shared plan / write step ---------------------------------------------


def _user_raw(lib, kind, slug):
    """The raw user-tier file for `slug`, or None. Same helper the bundle
    importer uses to decide identical/exists/overwrite."""
    path = (lib.user_root / kind / f"{slug}.json") if lib.user_root else None
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def apply_drafts(lib, drafts, warnings, *, source, overwrite=False, dry_run=False):
    """Validate every draft, then write the ones that are not in the way.

    The report is `import_bundle`'s plan shape verbatim — written / skipped /
    missing_factory / loras / dry_run, plus needs_overwrite — so the
    Composer's existing plan card renders it unchanged. `warnings` and
    `source` are additive.
    """
    for kind, slug, data in drafts:
        pl.validate_slug(slug)
        parser = pl.parse_section if kind == "sections" else pl.parse_template
        parser(data, slug, f"import:{slug}")  # never write a file the engine can't read
    report = {
        "written": [],
        "skipped": [],
        "missing_factory": [],
        "loras": [],
        "dry_run": bool(dry_run),
        "source": str(source),
        "warnings": [],
        "planned_files": len(drafts),
    }
    for kind, slug, data in sorted(drafts, key=lambda draft: (draft[0], draft[1])):
        singular = "section" if kind == "sections" else "template"
        existing = _user_raw(lib, kind, slug)
        if existing == data:
            report["skipped"].append({"kind": singular, "slug": slug, "reason": "identical"})
            continue
        if existing is not None and not overwrite:
            report["skipped"].append({"kind": singular, "slug": slug, "reason": "exists"})
            report["needs_overwrite"] = True
            continue
        if not dry_run:
            lib.save_user(kind, slug, data)
        entry = {"kind": singular, "slug": slug, "overwrites": existing is not None}
        factory = (lib.factory_root / kind / f"{slug}.json").is_file()
        entry["extends_factory" if kind == "sections" else "shadows_factory"] = factory
        report["written"].append(entry)
    report["planned_items"] = sum(
        len(data.get("items") or ()) for kind, _slug, data in drafts if kind == "sections"
    )
    report["warnings"] = _capped(warnings)
    return report


def _capped(warnings):
    warnings = [str(w) for w in warnings]
    if len(warnings) <= MAX_WARNINGS:
        return warnings
    return [
        *warnings[:MAX_WARNINGS],
        f"… and {len(warnings) - MAX_WARNINGS} more warning(s) — fix these first and "
        "run the import again to see the rest",
    ]


def extract_wildcard_archive(archive, dest):
    """Unpack the wildcard files of a .zip into `dest`, and nothing else.

    An archive is UNTRUSTED input — it is downloaded from a model site or
    handed over by someone else — so this is an allowlist, not an unpack:

    * only WILDCARD_SUFFIXES entries are written at all; everything else
      (executables, .url files, nested archives, the readme's images) is
      ignored rather than extracted and then skipped later;
    * every name is re-derived from its own parts, so an absolute path, a
      drive letter, a '..' segment or a backslash separator cannot escape
      `dest` (zip-slip). ZipFile.extract sanitises too — this does not rely
      on that, because the check is one line and the failure is arbitrary
      file write;
    * directory entries and anything ZIP flags as a symlink are skipped: a
      symlink in an archive is a link into the reader's filesystem;
    * the caps are the folder importer's own, applied to the UNCOMPRESSED
      sizes declared in the central directory, so a zip bomb is refused
      before a byte is written. The declared size is then verified against
      what is actually read — a lying header is the other half of that trick.

    Returns (file count, warnings).
    """
    import zipfile

    warnings = []
    written = 0
    total = 0
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            wanted = [
                info for info in members if Path(info.filename).suffix.lower() in WILDCARD_SUFFIXES
            ]
            if not wanted:
                raise ImporterError(
                    f"'{Path(archive).name}' contains no {' / '.join(WILDCARD_SUFFIXES)} file",
                    "that archive is not a wildcard pack — open it and check, or point "
                    "the import at the folder you extracted it to",
                    404,
                )
            if len(wanted) > MAX_WILDCARD_FILES:
                raise ImporterError(
                    f"'{Path(archive).name}' holds {len(wanted)} wildcard files, over the "
                    f"{MAX_WILDCARD_FILES} limit",
                    LIMIT_REMEDIATION,
                )
            declared = sum(info.file_size for info in wanted)
            if declared > MAX_WILDCARD_BYTES:
                raise ImporterError(
                    f"'{Path(archive).name}' unpacks to {declared / 1024 / 1024:.1f} MB of "
                    f"wildcard text, over the {MAX_WILDCARD_BYTES / 1024 / 1024:.0f} MB limit",
                    LIMIT_REMEDIATION,
                )
            for info in wanted:
                # 0xA000 in the high 16 bits of external_attr is S_IFLNK
                if (info.external_attr >> 16) & 0xF000 == 0xA000:
                    warnings.append(f"skipped '{info.filename}': it is a symlink")
                    continue
                # Split on the separator MYSELF instead of via Path: on Windows
                # Path('C:/x').parts is ('C:\\', 'x') — a drive part that ends
                # in a separator, not a colon — and joinpath() with an anchored
                # part DISCARDS the base, which is a write to C:\ (found by the
                # hostile-archive test, not by reading this code).
                parts = [
                    part
                    for part in info.filename.replace("\\", "/").split("/")
                    if part not in ("", ".", "..") and ":" not in part
                ]
                if not parts:
                    warnings.append(f"skipped '{info.filename}': unusable name")
                    continue
                if "/".join(parts) != info.filename.replace("\\", "/"):
                    # It is now safe, but it was not written the way the archive
                    # asked. Say so: an entry named '../x.txt' or 'C:/x.txt' is
                    # either hostile or broken, and silently importing it under
                    # a tidied name would hide both.
                    warnings.append(
                        f"'{info.filename}': imported as '{'/'.join(parts)}' — the archive "
                        "named it outside its own folder"
                    )
                if info.file_size > MAX_FILE_BYTES:
                    warnings.append(
                        f"skipped '{info.filename}': larger than "
                        f"{MAX_FILE_BYTES / 1024 / 1024:.0f} MB"
                    )
                    continue
                target = dest.joinpath(*parts)
                # Belt and braces: the filter above is a rule ABOUT names, this
                # is a fact about the resulting path. Only one of them has to
                # hold for the extraction to stay inside dest.
                if not target.resolve().is_relative_to(dest.resolve()):
                    warnings.append(f"skipped '{info.filename}': it points outside the archive")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src:
                    data = src.read(info.file_size + 1)
                if len(data) > info.file_size:
                    # the central directory under-reported this entry, which is
                    # how a bomb gets past a size pre-check
                    warnings.append(f"skipped '{info.filename}': larger than its zip header says")
                    continue
                total += len(data)
                if total > MAX_WILDCARD_BYTES:
                    raise ImporterError(
                        f"'{Path(archive).name}' unpacks to more than "
                        f"{MAX_WILDCARD_BYTES / 1024 / 1024:.0f} MB",
                        LIMIT_REMEDIATION,
                    )
                target.write_bytes(data)
                written += 1
    except zipfile.BadZipFile:
        raise ImporterError(
            f"'{Path(archive).name}' is not a readable zip archive",
            "re-download it — a Civitai download that needed an API key returns an "
            "HTML page with a .zip name, which is the usual cause",
        ) from None
    except OSError as exc:
        raise ImporterError(f"could not read '{Path(archive).name}': {exc}", "") from None
    if not written:
        raise ImporterError(
            f"'{Path(archive).name}' held no usable wildcard file",
            "every entry was skipped — see the warnings",
            404,
        )
    return written, warnings


def import_wildcards(lib, path, *, overwrite=False, dry_run=False):
    """Import a folder of wildcard files — or a .zip of them, which is how
    every published pack is actually distributed."""
    import tempfile

    raw = path.strip().strip('"').strip("'") if isinstance(path, str) else path
    if isinstance(raw, str) and raw.lower().endswith(".zip"):
        archive = resolve_source(raw, kind="file")
        with tempfile.TemporaryDirectory(prefix="mrln-wildcards-") as tmp:
            root = Path(tmp)
            _, warnings = extract_wildcard_archive(archive, root)
            drafts, more = wildcard_drafts(root)
            # the archive path is what the user recognises; the temp dir is
            # noise they never chose
            return apply_drafts(
                lib,
                drafts,
                warnings + more,
                source=str(archive),
                overwrite=overwrite,
                dry_run=dry_run,
            )
    root = resolve_source(path, kind="folder")
    drafts, warnings = wildcard_drafts(root)
    if not drafts:
        raise ImporterError(
            f"no importable wildcard file found under '{root}'",
            "point the import at the folder that actually holds the .txt/.yaml files "
            "(subfolders are read, but symlinked ones are not followed)",
            404,
        )
    return apply_drafts(
        lib, drafts, warnings, source=str(root), overwrite=overwrite, dry_run=dry_run
    )


def import_styles(lib, path, *, overwrite=False, dry_run=False):
    file_path = resolve_source(path, kind="file")
    drafts, warnings = styles_drafts(file_path)
    return apply_drafts(
        lib, drafts, warnings, source=str(file_path), overwrite=overwrite, dry_run=dry_run
    )


# -- endpoints ----------------------------------------------------------------


@_guarded
def handle_import_wildcards(lib, payload):
    """POST {path, dry_run?, overwrite?} — read a folder of .txt/.yaml
    wildcard files into user sections under 'wildcards/…'.

    Answers import_bundle's plan shape (+ warnings), so dry_run=true feeds the
    Composer's existing import-plan card. Nothing is written unless every
    planned file parses."""
    try:
        report = import_wildcards(
            lib,
            payload.get("path"),
            overwrite=bool(payload.get("overwrite")),
            dry_run=bool(payload.get("dry_run")),
        )
    except ImporterError as exc:
        return exc.status, exc.body()
    report["fingerprint"] = lib.fingerprint()
    return 200, report


@_guarded
def handle_import_styles(lib, payload):
    """POST {path, dry_run?, overwrite?} — read an A1111 styles.csv into user
    templates ('styles/<name>' for a '{prompt}' row) and user items (section
    'styles/a1111' for the rest).

    Same plan shape and same all-or-nothing validation as
    handle_import_wildcards."""
    try:
        report = import_styles(
            lib,
            payload.get("path"),
            overwrite=bool(payload.get("overwrite")),
            dry_run=bool(payload.get("dry_run")),
        )
    except ImporterError as exc:
        return exc.status, exc.body()
    report["fingerprint"] = lib.fingerprint()
    return 200, report
