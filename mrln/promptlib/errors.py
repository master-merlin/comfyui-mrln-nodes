"""Exception hierarchy. Messages are user-facing: they surface verbatim in
the ComfyUI error toast, so every one includes remediation."""

import difflib


def _avail(names, limit=20):
    names = sorted(names)
    shown = ", ".join(names[:limit])
    if len(names) > limit:
        shown += f", … ({len(names) - limit} more)"
    return shown or "none"


class PromptLibError(Exception):
    """Base for all prompt-library errors."""


class SchemaError(PromptLibError):
    def __init__(self, source, message):
        super().__init__(f"{source}: {message}")
        self.source = source


class SectionNotFoundError(PromptLibError):
    def __init__(self, slug, available):
        super().__init__(
            f"section '{slug}' not found — import it into your user library or pick "
            f"another (available: {_avail(available)})"
        )
        self.slug = slug


class TemplateNotFoundError(PromptLibError):
    def __init__(self, slug, available, search_dirs=()):
        where = f" (searched: {', '.join(str(d) for d in search_dirs)})" if search_dirs else ""
        super().__init__(f"template '{slug}' not found{where} — available: {_avail(available)}")
        self.slug = slug


class ItemNotFoundError(PromptLibError):
    def __init__(self, scope, item, available):
        super().__init__(
            f"item '{item}' is not inside '{scope}' — pick one of: {_avail(available)}, or 'random'"
        )
        self.scope = scope
        self.item = item


class SelectionError(PromptLibError):
    def __init__(self, line, message):
        super().__init__(f"selection line {line!r}: {message}")
        self.line = line


class UnknownVariableError(PromptLibError):
    def __init__(self, name, available):
        available = [str(a) for a in available]
        close = difflib.get_close_matches(str(name), available, 1, 0.5)
        did = f" Did you mean '{{{close[0]}}}'?" if close else ""
        super().__init__(
            f"unknown placeholder '{{{name}}}' — no template variable, 'name=value' "
            f"line, or slot id (slot ids weave that draw inline) matches.{did} "
            f"(known: {_avail(available)})"
        )
        self.name = name


class WildcardSyntaxError(PromptLibError):
    def __init__(self, text, pos, message):
        snippet = text[max(0, pos - 20) : pos + 20].replace("\n", " ")
        super().__init__(f"wildcard syntax error at position {pos} ('…{snippet}…'): {message}")
        self.pos = pos


class RecursionLimitError(PromptLibError):
    def __init__(self):
        super().__init__(
            "variable/wildcard expansion exceeded depth 10 — check for a variable cycle"
        )


class RenderError(PromptLibError):
    pass
