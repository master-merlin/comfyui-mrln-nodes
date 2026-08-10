"""Unit tests for the naming/branding helpers in mrln/pack.py."""

from mrln.pack import _spaced, build_mappings, category, display, node_id


def test_node_id_prefix():
    assert node_id("ImageResize") == "MRLN_ImageResize"


def test_display_marker():
    assert display("Image Resize") == "Image Resize (MRLN)"


def test_category_root():
    assert category() == "MRLN"


def test_category_domain():
    assert category("image") == "MRLN/image"


def test_category_nested():
    assert category("masking", "generate") == "MRLN/masking/generate"


def test_spaced_camel_case():
    assert _spaced("ImageResize") == "Image Resize"


def test_spaced_keeps_acronym_runs():
    assert _spaced("HDRPreview") == "HDRPreview"
    assert _spaced("LoadHDRImage") == "Load HDRImage"


class _Dummy:
    pass


class _Other:
    pass


def test_build_mappings_auto_label():
    classes, displays = build_mappings({"ImageResize": _Dummy})
    assert classes == {"MRLN_ImageResize": _Dummy}
    assert displays == {"MRLN_ImageResize": "Image Resize (MRLN)"}


def test_build_mappings_explicit_label():
    classes, displays = build_mappings({"ImageRGBSplit": (_Other, "Split RGB Channels")})
    assert classes == {"MRLN_ImageRGBSplit": _Other}
    assert displays == {"MRLN_ImageRGBSplit": "Split RGB Channels (MRLN)"}
