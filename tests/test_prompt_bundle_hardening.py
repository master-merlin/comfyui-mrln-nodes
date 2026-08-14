"""A bundle is a file from a stranger.

The point of bundles is that people share them — on Civitai, in a Discord, next
to a workflow. That makes an imported bundle UNTRUSTED INPUT, and the two
questions worth answering before shipping the feature are:

1. can a hostile bundle write, or fetch, anything the user did not ask for?
2. does an honest bundle actually reproduce the sender's render?

The first is security, the second is the whole promise. Both are asserted here
rather than assumed.
"""

import contextlib
import json

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_roots

from mrln.promptapi.core import ApiError
from mrln.promptapi.lora import _sanitize_lora_filename, _sanitize_subfolder, parse_air
from mrln.promptlib import (
    Library,
    PromptLibError,
    export_bundle,
    import_bundle,
    render,
    resolve_template,
)


@pytest.fixture()
def lib(tmp_path):
    return Library(*build_roots(tmp_path))


def _bundle(slug, section_slug=None):
    section_slug = section_slug or slug
    return {
        "format": "mrln-bundle",
        "bundle_version": 1,
        "kind": "section",
        "slug": slug,
        "sections": {section_slug: {"label": "x", "items": [{"name": "a", "text": "a"}]}},
    }


# -- 1. a hostile bundle cannot escape the user library ----------------------

# Every one of these is a real way a path has escaped a sandbox somewhere.
HOSTILE_SLUGS = [
    "../../../../evil",  # the classic
    "/etc/evil",  # absolute, posix
    "C:/Windows/evil",  # absolute, windows
    "..\\..\\evil",  # backslash separators
    "a/../../evil",  # traversal mid-path
    "ok\x00/evil",  # NUL truncation
    "evil...",  # trailing dots (windows strips them)
    "CON",  # reserved device name (windows)
    "\uff0e\uff0e/evil",  # fullwidth dots, in case a normaliser folds them
]


@pytest.mark.parametrize("slug", HOSTILE_SLUGS)
def test_a_hostile_slug_never_writes_a_file(lib, slug, tmp_path):
    before = sorted(p for p in tmp_path.rglob("*.json"))
    with pytest.raises(PromptLibError):
        import_bundle(lib, _bundle(slug), dry_run=False)
    assert sorted(p for p in tmp_path.rglob("*.json")) == before, (
        f"importing a bundle slugged {slug!r} changed the filesystem"
    )


@pytest.mark.parametrize("slug", HOSTILE_SLUGS)
def test_a_hostile_section_key_is_rejected_too(lib, slug):
    """Not just the bundle's own slug — every section key it carries."""
    with pytest.raises(PromptLibError):
        import_bundle(lib, _bundle("fine", section_slug=slug), dry_run=False)


# -- 2. a hostile bundle cannot aim a download ------------------------------


@pytest.mark.parametrize(
    "air",
    [
        "urn:air:sd1:lora:evilhost:1@2",  # not civitai
        "https://evil.example/payload.safetensors",  # not an AIR at all
        "urn:air:sd1:lora:civitai:../../1@2",  # traversal in the id
        "urn:air:sd1:lora:civitai:1@2 file:///etc/passwd",  # trailing smuggle
        "urn:air:sd1:lora:civitai:abc@def",  # non-numeric ids
        "",
    ],
)
def test_only_a_real_civitai_air_can_start_a_download(air):
    """parse_air is the gate: no parse, no download. A shared bundle can
    therefore never point this pack at an attacker's host."""
    assert parse_air(air) is None


def test_a_valid_air_is_two_integers():
    assert parse_air("urn:air:sd1:lora:civitai:4982@60568") == (4982, 60568)


@pytest.mark.parametrize("folder", ["../../..", "C:/Windows", "a/../../b", "ok/../evil", "....//"])
def test_a_download_folder_cannot_escape_the_loras_root(folder):
    # ApiError specifically: a refusal the user reads, not a stack trace
    with pytest.raises(ApiError):
        _sanitize_subfolder(folder)


def test_a_download_folder_keeps_an_honest_subfolder():
    assert _sanitize_subfolder("good/sub") == "good/sub"
    assert _sanitize_subfolder("/etc") == "etc"  # leading slash stripped, not escaped
    assert _sanitize_subfolder(None) == ""


@pytest.mark.parametrize(
    "name,expected",
    [
        ("../../evil.safetensors", "evil.safetensors"),  # directories stripped
        ("a/b.safetensors", "b.safetensors"),
        ("evil.exe", "evil.exe.safetensors"),  # cannot land an executable
        ("ok.safetensors", "ok.safetensors"),
    ],
)
def test_a_downloaded_file_is_always_a_safetensors_in_one_directory(name, expected):
    assert _sanitize_lora_filename(name) == expected


# -- 3. malformed bundles fail cleanly, not by exploding ---------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"format": "mrln-bundle", "bundle_version": 1, "kind": "section", "slug": "s"},
        {
            "format": "mrln-bundle",
            "bundle_version": 1,
            "kind": "section",
            "slug": "s",
            "sections": [],
        },
        {
            "format": "mrln-bundle",
            "bundle_version": 1,
            "kind": "section",
            "slug": "s",
            "sections": "no",
        },
        {
            "format": "mrln-bundle",
            "bundle_version": 1,
            "kind": "section",
            "slug": 42,
            "sections": {},
        },
        {
            "format": "mrln-bundle",
            "bundle_version": 1,
            "kind": "section",
            "slug": "s",
            "sections": {"s": {"items": "not-a-list"}},
        },
        {
            "format": "mrln-bundle",
            "bundle_version": 1,
            "kind": "section",
            "slug": "s",
            "sections": {"s": {"items": [{"name": "a", "text": 5}]}},
        },
    ],
)
def test_a_malformed_bundle_raises_a_library_error(lib, payload):
    """A PromptLibError reaches the user as a message. Anything else — a
    TypeError, a KeyError — reaches them as a stack trace."""
    with pytest.raises(PromptLibError):
        import_bundle(lib, payload, dry_run=False)


def test_a_deeply_nested_bundle_does_not_hang(lib):
    """A bundle is a JSON file someone else wrote; depth is theirs to choose."""
    section = {"label": "deep", "items": [{"name": "a", "text": "{x}" * 200}]}
    payload = {
        "format": "mrln-bundle",
        "bundle_version": 1,
        "kind": "section",
        "slug": "deep",
        "sections": {"deep": section},
    }
    # refusing is fine; hanging, or recursing to death, is not
    with contextlib.suppress(PromptLibError):
        import_bundle(lib, payload, dry_run=True)


# -- 4. the promise: an honest bundle reproduces the sender's render ---------


def test_a_bundle_reproduces_the_senders_render_exactly(tmp_path):
    """The reason to share one at all. Same seed, same prompt, on a machine
    that has only the factory library and the bundle."""
    sender = Library(*build_roots(tmp_path / "a"))
    sender.save_user(
        "sections",
        "mine/flourish",
        {
            "label": "Flourish",
            "items": [
                {"name": "gilt", "text": "gilt edges"},
                {"name": "matte", "text": "a matte wash"},
            ],
        },
    )
    sender.save_user(
        "templates",
        "mine/share",
        {
            "label": "Share",
            "prefix": "a study,",
            "slots": [
                {"id": "paint", "ref": "color"},  # factory section
                {"id": "extra", "ref": "mine/flourish"},  # user section, must travel
            ],
        },
    )
    bundle = export_bundle(sender, "templates", "mine/share")

    # the receiving machine: same factory content, nothing of the sender's
    receiver = Library(build_roots(tmp_path / "b")[0], tmp_path / "b-user")
    import_bundle(receiver, json.loads(json.dumps(bundle)), dry_run=False)

    for seed in (0, 7, 1234567):
        a = render(
            resolve_template(
                sender,
                sender.load_template("mine/share"),
                seed=seed,
                mode="as configured",
                selection={},
                variables={},
            ),
            "string",
            sender.load_template("mine/share").render,
        )
        b = render(
            resolve_template(
                receiver,
                receiver.load_template("mine/share"),
                seed=seed,
                mode="as configured",
                selection={},
                variables={},
            ),
            "string",
            receiver.load_template("mine/share").render,
        )
        assert a.positive == b.positive, f"seed {seed} rendered differently after a round trip"
        assert a.choices == b.choices, f"seed {seed} drew different items after a round trip"


def test_a_bundle_carries_the_user_sections_and_not_the_factory_ones(tmp_path):
    """Small files are why this works over a chat client — factory content
    resolves on the far side instead of riding along."""
    sender = Library(*build_roots(tmp_path / "a"))
    sender.save_user(
        "sections", "mine/only", {"label": "Mine", "items": [{"name": "a", "text": "a"}]}
    )
    sender.save_user(
        "templates",
        "mine/t",
        {"label": "T", "slots": [{"id": "f", "ref": "lighting"}, {"id": "m", "ref": "mine/only"}]},
    )
    bundle = export_bundle(sender, "templates", "mine/t")
    assert "mine/only" in bundle["sections"]
    # lighting is factory-only in this fixture; color is NOT (the user tier
    # overrides it), and an overridden section rightly travels with the bundle
    assert "lighting" not in bundle["sections"], "a factory section was copied into the bundle"
    assert "lighting" in bundle.get("factory_refs", []), "the factory dependency was not recorded"
