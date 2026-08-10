"""Inline text expressions: {a|b|c} wildcards and {variable} substitution.

Grammar:
    text  := ( literal | "{{" | "}}" | group | var )*
    group := "{" alt ("|" alt)+ "}"     # >= 2 alternatives, may be empty, nest freely
    var   := "{" NAME "}"               # NAME = [A-Za-z_][A-Za-z0-9_]*

"{{"/"}}" are literal braces. A braced group WITHOUT "|" must be a known
variable (silent passthrough would hide typos). SD emphasis "(text:1.2)"
contains no braces and passes through untouched. Variables expand first,
then wildcards inside their values (recursive, depth-capped).
"""

import re

from .errors import RecursionLimitError, UnknownVariableError, WildcardSyntaxError
from .seeding import weighted_index

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_DEPTH = 10


class _Group:
    __slots__ = ("alts",)

    def __init__(self, alts):
        self.alts = alts  # list of node-lists


class _Var:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name


def parse(text):
    nodes, pos = _parse_nodes(text, 0, top=True)
    return nodes


def _parse_nodes(text, pos, *, top):
    """Parse until end (top) or an unescaped '}' / top-level '|' (in groups)."""
    nodes = []
    literal = []
    while pos < len(text):
        ch = text[pos]
        if ch == "{":
            if text.startswith("{{", pos):
                literal.append("{")
                pos += 2
                continue
            if literal:
                nodes.append("".join(literal))
                literal = []
            node, pos = _parse_braced(text, pos)
            nodes.append(node)
            continue
        if ch == "}":
            if text.startswith("}}", pos):
                literal.append("}")
                pos += 2
                continue
            if top:
                raise WildcardSyntaxError(
                    text, pos, "unbalanced '}' (use '}}' for a literal brace)"
                )
            break  # group closer, handled by caller
        if ch == "|" and not top:
            break  # alternative separator, handled by caller
        literal.append(ch)
        pos += 1
    if literal:
        nodes.append("".join(literal))
    return nodes, pos


def _parse_braced(text, open_pos):
    """Parse '{...}' starting at open_pos; returns (_Group | _Var, next_pos)."""
    alts = []
    pos = open_pos + 1
    while True:
        nodes, pos = _parse_nodes(text, pos, top=False)
        alts.append(nodes)
        if pos >= len(text):
            raise WildcardSyntaxError(text, open_pos, "unclosed '{' (use '{{' for a literal brace)")
        if text[pos] == "|":
            pos += 1
            continue
        pos += 1  # consume '}'
        break
    if len(alts) > 1:
        return _Group(alts), pos
    # single alternative: must be a plain variable name
    inner = alts[0]
    if len(inner) == 1 and isinstance(inner[0], str) and _NAME_RE.match(inner[0]):
        return _Var(inner[0]), pos
    raise WildcardSyntaxError(
        text,
        open_pos,
        "brace group without '|' must be a {variable_name} (or escape braces as '{{ }}')",
    )


def expand(text, variables, rng, *, depth=0):
    """Resolve variables and wildcards in `text` deterministically via `rng`."""
    if depth > _MAX_DEPTH:
        raise RecursionLimitError()
    return _eval_nodes(parse(text), variables, rng, depth)


def _eval_nodes(nodes, variables, rng, depth):
    out = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, _Group):
            chosen = node.alts[weighted_index(rng, [1.0] * len(node.alts))]
            out.append(_eval_nodes(chosen, variables, rng, depth))
        else:  # _Var
            if node.name not in variables:
                raise UnknownVariableError(node.name, variables.keys())
            out.append(expand(str(variables[node.name]), variables, rng, depth=depth + 1))
    return "".join(out)
