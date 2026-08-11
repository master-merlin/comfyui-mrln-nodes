"""Trigger words from LoRA file metadata — safetensors header only, no
torch. The header is 8 bytes little-endian length + that many bytes of
JSON; trainers stash their metadata under "__metadata__". Direct phrase
keys (modelspec/ai-toolkit style) win; kohya-style trainers (incl. the
Civitai on-site trainer) get the most frequent dataset tag instead."""

import json
import struct
from pathlib import Path

_PHRASE_KEYS = ("modelspec.trigger_phrase", "trigger_phrase", "ss_trigger_phrase")
_MAX_HEADER = 64 * 1024 * 1024  # anything larger is not a real header


def read_safetensors_metadata(path):
    """-> the '__metadata__' dict of a .safetensors file ({} if absent).
    Raises ValueError for non-safetensors or corrupt files."""
    path = Path(path)
    if path.suffix.lower() != ".safetensors":
        raise ValueError(f"'{path.name}': only .safetensors files carry readable metadata")
    with path.open("rb") as fh:
        raw = fh.read(8)
        if len(raw) != 8:
            raise ValueError(f"'{path.name}' is not a valid safetensors file (truncated)")
        (length,) = struct.unpack("<Q", raw)
        if not 0 < length <= _MAX_HEADER:
            raise ValueError(f"'{path.name}' has an implausible header size — corrupt file?")
        header = fh.read(length)
    if len(header) != length:
        raise ValueError(f"'{path.name}' is not a valid safetensors file (truncated)")
    try:
        parsed = json.loads(header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"'{path.name}' safetensors header is not valid JSON ({exc})") from None
    meta = parsed.get("__metadata__", {}) if isinstance(parsed, dict) else {}
    return meta if isinstance(meta, dict) else {}


def trigger_from_metadata(meta):
    """-> (trigger, source_key) or (None, None). Phrase keys beat tag
    frequency; within ss_tag_frequency all datasets merge and the most
    frequent tag wins (the training-caption convention)."""
    for key in _PHRASE_KEYS:
        value = str(meta.get(key) or "").strip()
        if value:
            return value, key
    freq_raw = meta.get("ss_tag_frequency")
    if freq_raw:
        try:
            datasets = json.loads(freq_raw) if isinstance(freq_raw, str) else freq_raw
            counts = {}
            for tags in datasets.values():
                for tag, count in tags.items():
                    tag = str(tag).strip()
                    if tag:
                        counts[tag] = counts.get(tag, 0) + int(count)
            if counts:
                return max(counts.items(), key=lambda kv: kv[1])[0], "ss_tag_frequency"
        except (ValueError, TypeError, AttributeError):
            pass
    return None, None
