"""Writes a small factory+user fixture library into a tmp dir. Fixtures live
next to assertions — no static fixture dirs to drift."""

import json
from pathlib import Path

import support  # noqa: F401  (ensures repo root on sys.path)

from mrln.promptlib import Library


def _write(root: Path, rel: str, obj: dict):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_roots(tmp_path: Path):
    factory = tmp_path / "factory"
    user = tmp_path / "user"

    _write(
        factory,
        "sections/color.json",
        {
            "label": "Color",
            "items": [
                {"name": "red", "text": "bright red"},
                {"name": "green", "text": "deep green"},
                {"name": "blue", "text": "ocean blue", "negative": "muddy tones"},
                {
                    "name": "gold",
                    "text": "shimmering gold",
                    "weight": 3.0,
                    "data": {"hex": ["#FFD700"]},
                },
            ],
        },
    )
    _write(
        factory,
        "sections/location/urban.json",
        {
            "label": "Urban Location",
            "items": [
                {"name": "shibuya", "text": "Shibuya Crossing", "tags": ["city"]},
                {
                    "name": "neon-alley",
                    "text": "neon-lit alley, wet asphalt",
                    "negative": "daylight",
                    "tags": ["neon"],
                    "requires": ["night"],
                },
            ],
        },
    )
    _write(
        factory,
        "sections/location/nature.json",
        {
            "label": "Nature Location",
            "items": [
                {"name": "alpine-pass", "text": "alpine mountain pass"},
                {"name": "desert-road", "text": "endless desert road"},
            ],
        },
    )
    _write(
        factory,
        "sections/lighting.json",
        {
            "label": "Lighting",
            "negative": "flat lighting",
            "items": [
                {"name": "daylight", "text": "bright daylight", "tags": ["daylight"]},
                {"name": "night", "text": "moonlit night", "tags": ["night"]},
            ],
        },
    )
    _write(
        factory,
        "templates/basic.json",
        {
            "label": "Basic",
            "prefix": "photo of a {trigger}",
            "suffix": "high quality",
            "negative": "lowres",
            "variables": [{"name": "trigger", "default": "sports car"}],
            "slots": [
                {"id": "paint", "ref": "color", "default": "red"},
                {"id": "location", "ref": "location", "default": "urban/shibuya", "label": "Place"},
                {"id": "lighting", "ref": "lighting", "default": "random"},
                {
                    "id": "extra",
                    "ref": "color",
                    "default": "random",
                    "allow_empty": True,
                    "empty_weight": 100.0,
                    "emphasis": 1.3,
                },
            ],
            "render": {"format": "string", "joiner": ", "},
        },
    )
    _write(
        factory,
        "templates/varianted.json",
        {
            "label": "Varianted",
            "slots": [{"id": "paint", "ref": "color", "default": "blue"}],
            "variants": [
                {
                    "name": "studio",
                    "slots": [{"id": "backdrop", "ref": "location/urban", "default": "shibuya"}],
                },
                {
                    "name": "outdoor",
                    "slots": [{"id": "backdrop", "ref": "location/nature", "default": "random"}],
                },
            ],
            "variant_default": "studio",
            "order": ["@variant", "paint"],
            "render": {"format": "string_labeled"},
        },
    )

    # user tier: overrides color.json (adds an item), adds one section
    _write(
        user,
        "sections/color.json",
        {
            "label": "Color (mine)",
            "items": [
                {"name": "red", "text": "bright red"},
                {"name": "green", "text": "deep green"},
                {"name": "blue", "text": "ocean blue"},
                {"name": "gold", "text": "shimmering gold"},
                {"name": "petrol", "text": "dark petrol"},
            ],
        },
    )
    _write(
        user,
        "sections/mood.json",
        {
            "items": [{"name": "epic", "text": "epic composition"}],
        },
    )
    return factory, user


def build_library(tmp_path: Path) -> Library:
    return Library(*build_roots(tmp_path))


def factory_only_library(tmp_path: Path) -> Library:
    factory, _ = build_roots(tmp_path)
    return Library(factory, None)
