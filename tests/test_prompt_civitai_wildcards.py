"""Civitai's 'Wildcards' model type -> the wildcard importer.

Nothing here touches the network: urlopen is replaced with a fake that answers
from canned payloads shaped like the real API responses (checked against
civitai.com/api/v1/models?types=Wildcards while this was written).

The tests that matter most are the refusals. This endpoint takes a link from a
user, reaches the internet, downloads an archive and unpacks it — four things
that each want a reason to stop.
"""

import hashlib
import io
import json
import urllib.error
import zipfile

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln.promptapi import civitai

PACK = {
    "photographers.txt": "by Ansel Adams\nby Diane Arbus\n",
    "moods/weather.txt": "3::thunderhead light\novercast\n",
}


def make_archive():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in PACK.items():
            zf.writestr(name, text)
    return buf.getvalue()


def model_payload(archive, **over):
    body = {
        "id": 615967,
        "name": "PonyXL Wildcards Vault",
        "type": "Wildcards",
        "creator": {"username": "navimixu"},
        "allowNoCredit": True,
        "allowDerivatives": False,
        "allowCommercialUse": ["RentCivit", "Rent"],
        "allowDifferentLicense": True,
        "modelVersions": [
            {
                "id": 1234,
                "name": "Artstyles III",
                "files": [
                    {
                        "name": "vault_artstylesIII.zip",
                        "primary": True,
                        "downloadUrl": "https://civitai.com/api/download/models/1234",
                        "hashes": {"SHA256": hashlib.sha256(archive).hexdigest().upper()},
                    }
                ],
            }
        ],
    }
    body.update(over)
    return body


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


@pytest.fixture()
def wired(monkeypatch):
    """Patch BOTH network seams and record what was requested."""
    archive = make_archive()
    state = {"model": model_payload(archive), "archive": archive, "urls": [], "headers": []}

    def fake_urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        state["urls"].append(url)
        state["headers"].append(dict(getattr(request, "headers", {})))
        if "/api/v1/models/" in url:
            return FakeResponse(json.dumps(state["model"]).encode())
        if "/api/v1/model-versions/" in url:
            version = dict(state["model"]["modelVersions"][0])
            version["modelId"] = state["model"]["id"]
            return FakeResponse(json.dumps(version).encode())
        return FakeResponse(state["archive"])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return state


# -- link parsing -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://civitai.com/models/615967/ponyxl-vault", (615967, None)),
        ("https://civitai.com/models/615967?modelVersionId=1234", (615967, 1234)),
        ("https://www.civitai.com/models/615967", (615967, None)),
        ("https://civitai.com/model-versions/98765", (None, 98765)),
        ("urn:air:sdxl:wildcards:civitai:615967@1234", (615967, 1234)),
        ("615967", (615967, None)),
        ("615967@1234", (615967, 1234)),
    ],
)
def test_every_shape_a_user_might_paste(raw, expected):
    assert civitai.parse_model_ref(raw) == expected


def test_a_link_to_another_host_is_refused_rather_than_reinterpreted():
    """Reading the id out of any URL would be SAFE — only the id is ever used,
    and it goes into our own endpoint — but silently importing Civitai model 1
    because the link said example.com/models/1 is a different pack than the one
    the user asked for."""
    for bad in ("https://example.com/models/1", "https://civitai.com.attacker.net/models/9"):
        with pytest.raises(civitai.CivitaiError) as err:
            civitai.parse_model_ref(bad)
        assert "not civitai.com" in str(err.value)


def test_nonsense_is_refused_with_the_shape_it_wanted():
    with pytest.raises(civitai.CivitaiError) as err:
        civitai.parse_model_ref("wildcards please")
    assert "civitai.com/models/" in err.value.remediation


# -- the happy path -----------------------------------------------------------


def test_a_wildcards_pack_imports_into_user_sections(lib, wired):
    report = civitai.import_civitai_wildcards(lib, "https://civitai.com/models/615967")
    slugs = [w["slug"] for w in report["written"]]
    assert slugs == ["wildcards/moods/weather", "wildcards/photographers"]
    assert report["dry_run"] is True  # the endpoint plans first, always
    assert report["source"] == "civitai:615967@1234"
    assert report["civitai"]["creator"] == "navimixu"
    assert report["civitai"]["url"] == "https://civitai.com/models/615967"


def test_the_licence_leads_the_warnings_because_it_precedes_the_decision(lib, wired):
    report = civitai.import_civitai_wildcards(lib, "615967")
    assert "NO derivatives" in report["warnings"][0]
    assert "navimixu" in report["warnings"][0]
    assert report["civitai"]["licence"]["allow_derivatives"] is False
    assert report["civitai"]["licence"]["allow_commercial_use"] == ["RentCivit", "Rent"]


def test_the_api_key_rides_a_header_and_never_the_url(lib, wired, monkeypatch):
    monkeypatch.setattr(civitai, "_token", lambda _lib: "secret-key-value")
    civitai.import_civitai_wildcards(lib, "615967")
    assert any("Bearer secret-key-value" in str(h) for h in wired["headers"])
    assert not any("secret-key-value" in url for url in wired["urls"])


def test_a_pinned_version_is_the_one_fetched(lib, wired):
    newest = {
        "id": 9999,
        "name": "Artstyles IV",
        "files": list(wired["model"]["modelVersions"][0]["files"]),
    }
    wired["model"]["modelVersions"].insert(0, newest)
    report = civitai.import_civitai_wildcards(
        lib, "https://civitai.com/models/615967?modelVersionId=1234"
    )
    assert report["civitai"]["version_id"] == 1234
    assert report["civitai"]["version"] == "Artstyles III"


# -- refusals -----------------------------------------------------------------


def test_a_model_that_is_not_a_wildcards_pack_is_refused_before_downloading(lib, wired):
    """A checkpoint link would otherwise start streaming 6 GB of .safetensors
    because the user pasted the wrong tab."""
    wired["model"]["type"] = "Checkpoint"
    with pytest.raises(civitai.CivitaiError) as err:
        civitai.import_civitai_wildcards(lib, "615967")
    assert "not a Wildcards pack" in str(err.value)
    assert not any("download" in url for url in wired["urls"])


def test_a_corrupted_download_is_refused_by_its_hash(lib, wired):
    wired["model"]["modelVersions"][0]["files"][0]["hashes"]["SHA256"] = "00" * 32
    with pytest.raises(civitai.CivitaiError) as err:
        civitai.import_civitai_wildcards(lib, "615967")
    assert "SHA256" in str(err.value) and err.value.status == 502


def test_a_login_page_under_a_zip_name_names_the_api_key(lib, wired):
    """The real failure when a pack needs an account: Civitai answers HTML."""
    from mrln.promptapi.importers import ImporterError

    wired["archive"] = b"<!DOCTYPE html><html>log in</html>"
    wired["model"]["modelVersions"][0]["files"][0]["hashes"] = {}
    with pytest.raises(ImporterError) as err:
        civitai.import_civitai_wildcards(lib, "615967")
    assert "API key" in err.value.remediation


def test_a_missing_model_says_so_with_404(lib, monkeypatch):
    def raise_404(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", raise_404)
    with pytest.raises(civitai.CivitaiError) as err:
        civitai.import_civitai_wildcards(lib, "615967")
    assert err.value.status == 404


def test_an_unauthorised_fetch_points_at_the_settings_tab(lib, monkeypatch):
    def raise_401(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", raise_401)
    with pytest.raises(civitai.CivitaiError) as err:
        civitai.import_civitai_wildcards(lib, "615967")
    assert err.value.status == 403 and "Settings tab" in err.value.remediation


def test_a_version_without_an_archive_is_refused(lib, wired):
    wired["model"]["modelVersions"][0]["files"] = [
        {"name": "preview.png", "downloadUrl": "https://civitai.com/x"}
    ]
    with pytest.raises(civitai.CivitaiError) as err:
        civitai.import_civitai_wildcards(lib, "615967")
    assert ".zip" in str(err.value)


# -- the endpoint -------------------------------------------------------------


def test_the_handler_defaults_to_a_dry_run(lib, wired):
    status, report = civitai.handle_import_civitai_wildcards(lib, {"url": "615967"})
    assert status == 200
    assert report["dry_run"] is True
    assert report["fingerprint"] == lib.fingerprint()
    assert not (lib.user_root / "sections" / "wildcards").exists()


def test_the_handler_writes_only_when_asked(lib, wired):
    status, report = civitai.handle_import_civitai_wildcards(
        lib, {"url": "615967", "dry_run": False}
    )
    assert status == 200 and report["dry_run"] is False
    assert (lib.user_root / "sections" / "wildcards" / "photographers.json").is_file()


def test_a_refusal_answers_with_a_status_and_a_remediation(lib):
    status, body = civitai.handle_import_civitai_wildcards(lib, {"url": ""})
    assert status == 400
    assert body["error"] and body["remediation"]


def test_the_route_is_a_body_reading_post():
    from mrln import promptapi

    table = {(m, p): (h, reads) for m, p, h, reads in promptapi.ROUTES}
    assert table[("post", "/mrln/prompt/import-civitai-wildcards")] == (
        promptapi.handle_import_civitai_wildcards,
        True,
    )
