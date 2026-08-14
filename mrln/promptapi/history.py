"""Generation history (SPEC 6.2): the settings gate, the record the Prompt
Template node writes after every render, and the two endpoints behind the
Composer's History tab.

`promptlib/store.py` owns the bytes on disk — user-tier
`<user>/history/render-YYYYMM.jsonl`, locked appends, a keyset cursor, and an
append that logs instead of raising. This module owns everything above it:
which setting switches recording on, how long month files are kept, WHAT one
record contains, and the HTTP surface.

WHY THE RECORD LOOKS LIKE THIS. Deterministic seeding already makes every past
render reproducible in principle, so a record does not have to store a render
— it has to name the inputs of exactly one `pl.compose()` call. It names ALL of
them, and the field NAMES are the ones `POST /mrln/prompt/preview` already
takes, so "restore into the Composer" is: drop the four output fields, post the
rest. (SPEC 6.2's field list is short of three of those inputs — see
RECORD_FIELDS.)

WHY NO SECRET CAN REACH A RECORD. Every field is assembled by
`render_record()` from render outputs through an explicit keyword list;
RECORD_FIELDS *is* that list, and no code path copies a settings value into a
record. settings.json is read here only for a bool and an int
(`history_enabled`, `history_months`), never for its keys.

Recording is best-effort by design: a failed history write must never break a
render that already succeeded, so `record_renders()` and `prune_history()`
swallow and log everything, on top of store's own guarantee.
"""

from datetime import datetime, timedelta

from .. import promptlib as pl
from ..pack import logger
from ..promptlib import store
from .core import _guarded
from .settings import _read_settings

# settings.json keys (user tier). Flat, like `civitai_api_key`: these are two
# scalars, not a subsystem block like `llm`.
DEFAULT_HISTORY_MONTHS = 12
HISTORY_LIMIT_DEFAULT = 100
# A page cap, not a retention cap: `limit` reaches into month files line by
# line, and an unbounded value would let one request parse a whole year.
HISTORY_LIMIT_MAX = 500

# Everything one line may contain — the closed set that makes the no-secrets
# property structural rather than a review habit.
#
# `ts`, `template`, `profile`, `seed`, `mode`, `selection`, `format`,
# `positive`, `negative`, `choices`, `loras` are SPEC 6.2's list. The three
# after it are the compose() inputs the spec's list forgot, and the feature
# does not work without them: `variables` carries {trigger} (a template whose
# prefix reads "photo of a {trigger}" renders something else without it),
# `text_length` picks long vs short item texts, and `conflict_policy` decides
# which side of a negative conflict wins. `batch` is grouping metadata, present
# only when a queue click rendered more than one item.
RECORD_FIELDS = (
    "ts",
    "template",
    "profile",
    "seed",
    "mode",
    "selection",
    "variables",
    "format",
    "text_length",
    "conflict_policy",
    "positive",
    "negative",
    "choices",
    "loras",
    "batch",
)
# The subset a restoring client feeds back into /preview (or the node's
# widgets). The rest is what the render produced.
RESTORE_FIELDS = (
    "template",
    "profile",
    "seed",
    "mode",
    "selection",
    "variables",
    "format",
    "text_length",
    "conflict_policy",
)


# -- settings ----------------------------------------------------------------


def history_enabled(settings):
    """Is recording on? Default TRUE — history is the feature that makes
    deterministic seeding usable, and an install that never opted in still
    wants "what did I render an hour ago" to have an answer. Tolerant of a
    hand-edited settings.json: the string "false" means false."""
    value = settings.get("history_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def history_months(settings):
    """How many month files to keep. Nonsense reverts to the default; store's
    prune treats <= 0 as "prune nothing", so a stray 0 cannot wipe history."""
    try:
        return int(settings.get("history_months", DEFAULT_HISTORY_MONTHS))
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_MONTHS


def history_thumbs(settings):
    """Is the mini thumbnail on a history row on? Default TRUE, opt-OUT: it
    costs nothing until a row is actually scrolled into view, and finding a
    prompt by the picture you remember is the whole reason it exists. Same
    string tolerance as history_enabled, for the same hand-edited file."""
    value = settings.get("history_thumbs", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def history_settings(settings):
    """The knobs, under their settings.json names — one shape for the
    settings endpoint, the history endpoint and the panel."""
    return {
        "history_enabled": history_enabled(settings),
        "history_months": history_months(settings),
        "history_thumbs": history_thumbs(settings),
    }


# -- the record --------------------------------------------------------------


def render_record(
    *,
    template,
    profile=pl.STANDARD,
    seed,
    mode,
    selection,
    variables,
    format,
    text_length,
    conflict_policy,
    positive,
    negative,
    choices,
    loras=(),
    batch=None,
):
    """One history line, keyword-only so a caller cannot fill the wrong field.

    `ts` is NOT taken here: `record_renders()` stamps it, because the whole
    batch has to be stamped consistently (see there).

    `selection` and `variables` are stored as objects rather than as the
    node's raw text: that is the exact dict `pl.compose()` consumed, so the
    round trip needs no re-parsing, and /preview's `_kv_map` accepts either
    shape anyway. A restoring UI joins them back into "name=value" lines.
    The trigger widget rides inside `variables` under the key 'trigger',
    exactly as the node merges it before composing."""
    record = {
        "template": str(template),
        "profile": str(profile or pl.STANDARD),
        "seed": int(seed),
        "mode": str(mode),
        "selection": {str(k): str(v) for k, v in dict(selection or {}).items()},
        "variables": {str(k): str(v) for k, v in dict(variables or {}).items()},
        "format": str(format),
        "text_length": str(text_length),
        "conflict_policy": str(conflict_policy),
        "positive": str(positive),
        "negative": str(negative),
        "choices": str(choices),
        "loras": list(loras or ()),
    }
    if batch:
        record["batch"] = dict(batch)
    return record


def record_renders(lib, records):
    """Append `records` (oldest first) and return how many were written.

    NEVER raises. Two guards sit under this one — store.history_append
    swallows its own IO failures, and the node wraps the call again — because
    the render whose outputs these describe has ALREADY succeeded when this
    runs. Nothing here is allowed to cost more than the history line itself.

    TIMESTAMPS ARE STAMPED HERE, one `datetime.now()` for the whole batch plus
    one microsecond per item, and always with microsecond precision. That is
    not decoration: `store.history_read`'s `before` cursor is a lexicographic
    `ts` compare, so two records sharing a timestamp would make a page
    boundary silently skip one of them. Fixed-width microseconds keep
    lexicographic order == chronological order, and the +i offset keeps a
    64-item batch strictly ordered without depending on clock resolution.
    All items of one queue click still share the same second, which is what
    the History tab groups on."""
    try:
        records = list(records)
        if not records:
            return 0
        if not history_enabled(_read_settings(lib)):
            return 0
        now = datetime.now()
        for index, record in enumerate(records):
            stamp = (now + timedelta(microseconds=index)).isoformat(timespec="microseconds")
            # ts FIRST (and a fresh dict, so a caller's record is never
            # mutated): the line a human tails starts with when it happened
            store.history_append(lib, {"ts": stamp, **record})
        return len(records)
    except Exception as exc:
        logger.warning("MRLN prompt: history not recorded (%s)", exc)
        return 0


def prune_history(lib):
    """Drop month files beyond `history_months`. Never raises.

    Called once from the boot warm-up thread (`routes._warm_library_caches`),
    where the LoRA audit already runs: startup is when a retention setting is
    the user's current intent, the thread is already daemonised and
    exception-guarded, and a second thread for two unlink calls would be
    ceremony. Runs regardless of `history_enabled` — retention is about what
    is kept, not about whether new lines are written, so turning recording off
    must not freeze old months in place forever."""
    try:
        months = history_months(_read_settings(lib))
        before = len(store.history_files(lib))
        store.history_prune(lib, months)
        removed = before - len(store.history_files(lib))
        if removed > 0:
            logger.info(
                "MRLN prompt: pruned %d history month file(s), keeping the newest %d",
                removed,
                months,
            )
    except Exception:
        logger.debug("MRLN prompt: history prune skipped", exc_info=True)


# -- endpoints ---------------------------------------------------------------


def _limit(raw):
    try:
        return max(0, min(int(raw), HISTORY_LIMIT_MAX))
    except (TypeError, ValueError):
        return HISTORY_LIMIT_DEFAULT


@_guarded
def handle_history(lib, payload):
    """GET /mrln/prompt/history?limit=&before= — newest first.

    `before` is the keyset cursor: pass the `next_before` of the previous page
    and get the records strictly older than it. Reading limit+1 is what makes
    `has_more` honest without a second pass, and malformed lines are skipped
    by the store rather than failing the page — a half-written line from a
    killed process must never take the History tab down."""
    limit = _limit(payload.get("limit"))
    before = payload.get("before")
    before = before.strip() if isinstance(before, str) else ""
    records = store.history_read(lib, limit=limit + 1, before=before or None)
    has_more = len(records) > limit
    records = records[:limit]
    return 200, {
        "records": records,
        "limit": limit,
        "before": before,
        # empty when this was the last page: nothing left to ask for
        "next_before": str(records[-1].get("ts") or "") if has_more and records else "",
        "has_more": has_more,
        **history_settings(_read_settings(lib)),
    }


@_guarded
def handle_history_clear(lib, payload):
    """POST /mrln/prompt/history-clear {"confirm": true} — delete every month
    file.

    Destructive and unrecoverable, so it is an explicit action twice over: the
    panel arms the button and only the second click sends the request, and only
    JSON `true` counts here. GET query values are strings, so a bare cross-site
    GET, a prefetched link or a mistyped URL can never wipe a user's history —
    the same rule the LoRA download's `start` flag follows."""
    if payload.get("confirm") is not True:
        return 400, {
            "error": "clearing the render history needs an explicit confirmation",
            "remediation": 'POST {"confirm": true} as JSON — a query-string value is '
            "never a confirmation. In the Composer, click Clear history twice: the "
            "first click arms it.",
        }
    removed, failed = [], []
    for path in store.history_files(lib):
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            failed.append(f"{path.name}: {exc}")
    return 200, {
        "ok": not failed,
        "removed": removed,
        "count": len(removed),
        "failed": failed,
    }
