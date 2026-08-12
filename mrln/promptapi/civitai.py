"""Civitai's 'Wildcards' model type -> the wildcard importer.

The ecosystem publishes wildcard packs on Civitai as a `.zip` of `.txt` files
(796 models at the time of writing), which is precisely what
`importers.import_wildcards` consumes. This module is only the bridge: resolve
a link the user pasted to a model version, fetch its archive, hand the file
over. It never parses a wildcard line itself.

WHAT THIS MODULE REFUSES TO DO, and why:

- **It never fetches the URL it was given.** A pasted link is read for its
  numeric ids and nothing else; those ids go into THIS module's own constant
  endpoints. So a link cannot point the server at another host, which is the
  same rule `intake.py` follows for Civitai image links.
- **It refuses a model that is not type Wildcards.** A checkpoint's 6 GB
  .safetensors would otherwise start streaming because the caller pasted the
  wrong link. LoRAs have their own downloader; this one takes archives.
- **It surfaces the pack's licence flags instead of deciding about them.**
  `allowDerivatives: false` is common on real packs (the top-downloaded one
  has it). Importing into your own library is between you and the creator —
  but you should see the terms in the plan BEFORE anything is written, which
  is why they ride in the dry-run report rather than a log line.
- **Nothing is written to the library by the fetch.** The archive lands in a
  temp file, the importer plans from it, and `dry_run` still means dry: the
  same two-step every other importer here uses.

Secrets: the API key is read server-side per call, rides an Authorization
header (never a query, never a log), and every client-visible string produced
here goes through `lora._scrub_secrets`.
"""

import json
import re

from .core import ApiError, _guarded

# Constant endpoints. The only things interpolated are integers we parsed
# ourselves — see `parse_model_ref`.
MODEL_ENDPOINT = "https://civitai.com/api/v1/models/{model_id}"
VERSION_ENDPOINT = "https://civitai.com/api/v1/model-versions/{version_id}"

WILDCARD_TYPE = "wildcards"
ARCHIVE_SUFFIX = ".zip"

# A wildcard pack is text. The largest published ones are a few MB; this cap is
# the download's, sitting above importers.MAX_WILDCARD_BYTES so the importer's
# own limit is what a user normally meets (with its actionable message).
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
FETCH_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 180

KEY_REMEDIATION = (
    "some Civitai downloads need an account: add your API key in the Composer's "
    "Settings tab (it is stored server-side and never leaves this machine)"
)
LINK_REMEDIATION = (
    "paste the civitai.com link of a Wildcards model — for example "
    "https://civitai.com/models/615967 — or just its numeric id"
)

_MODEL_URL_RE = re.compile(r"/models/(\d+)")
_VERSION_QUERY_RE = re.compile(r"[?&]modelVersionId=(\d+)")
_AIR_RE = re.compile(r"urn:air:[^:]*:[^:]*:civitai:(\d+)(?:@(\d+))?", re.IGNORECASE)
_BARE_RE = re.compile(r"^\s*(\d+)(?:@(\d+))?\s*$")


class CivitaiError(ApiError):
    """A refused fetch, carrying its own status and remediation."""

    def __init__(self, message, remediation, status=400):
        super().__init__(message)
        self.remediation = remediation
        self.status = status

    def body(self):
        return {"error": str(self), "remediation": self.remediation}


# -- what the user pasted -----------------------------------------------------


def parse_model_ref(raw):
    """(model_id, version_id) from a link, an AIR or a bare id.

    Returns ints, never strings, because they are interpolated into an endpoint
    — parsing IS the sanitisation. A `modelVersionId` query pins one version of
    a multi-version pack, which matters: these packs ship 'Artstyles I / II /
    III' as separate versions of one model.
    """
    text = str(raw or "").strip()
    if not text:
        raise CivitaiError("missing required parameter 'url'", LINK_REMEDIATION)
    if "://" in text:
        # Reading the id out of ANY url would be safe — the id is all that is
        # ever used, and it goes into this module's own endpoint — but it would
        # also be dishonest: 'https://example.com/models/1' would quietly import
        # Civitai model 1, which is not what the link said. Refuse instead.
        import urllib.parse

        host = urllib.parse.urlsplit(text).hostname or ""
        if host.lower() not in ("civitai.com", "www.civitai.com"):
            raise CivitaiError(
                f"'{host or text[:60]}' is not civitai.com",
                "this importer only resolves civitai.com links; for wildcards from "
                "anywhere else, download the .zip and import the file directly",
            )
    bare = _BARE_RE.match(text)
    if bare:
        return int(bare.group(1)), int(bare.group(2)) if bare.group(2) else None
    air = _AIR_RE.search(text)
    if air:
        return int(air.group(1)), int(air.group(2)) if air.group(2) else None
    model = _MODEL_URL_RE.search(text)
    if not model:
        # A '/model-versions/<id>' link names a version and no model.
        version_only = re.search(r"/model-versions/(\d+)", text)
        if version_only:
            return None, int(version_only.group(1))
        raise CivitaiError(f"'{text[:120]}' is not a Civitai model link", LINK_REMEDIATION)
    version = _VERSION_QUERY_RE.search(text)
    return int(model.group(1)), int(version.group(1)) if version else None


# -- the API ------------------------------------------------------------------


def _token(lib):
    from .lora import _remember_secret
    from .settings import _read_settings

    return _remember_secret(str(_read_settings(lib).get("civitai_api_key") or ""))


def _get_json(url, token, *, what):
    import urllib.error
    import urllib.request

    from .lora import _scrub_secrets

    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CivitaiError(
                f"Civitai has no {what} with that id", LINK_REMEDIATION, 404
            ) from None
        if exc.code in (401, 403):
            raise CivitaiError(
                f"Civitai refused the {what} request ({exc.code})", KEY_REMEDIATION, 403
            ) from None
        raise CivitaiError(
            _scrub_secrets(f"Civitai answered {exc.code} for the {what}"),
            "try again in a moment — civitai.com may be rate-limiting or down",
            502,
        ) from None
    except Exception as exc:
        raise CivitaiError(
            _scrub_secrets(f"could not reach Civitai ({type(exc).__name__}: {exc})"),
            "check this machine's internet connection and any proxy settings",
            502,
        ) from None


def model_type_of(model):
    return str((model or {}).get("type") or "").strip().lower()


def licence_of(model):
    """The pack's terms, as flags plus one printable line.

    Reported, never enforced: whether importing someone's wildcards into your
    own library is within their licence is between you and them, and the honest
    thing a tool can do is put the terms in front of you first."""
    model = model or {}
    commercial = [str(x) for x in (model.get("allowCommercialUse") or [])]
    flags = {
        "allow_no_credit": bool(model.get("allowNoCredit")),
        "allow_derivatives": bool(model.get("allowDerivatives")),
        "allow_different_license": bool(model.get("allowDifferentLicense")),
        "allow_commercial_use": commercial,
    }
    parts = []
    parts.append("credit not required" if flags["allow_no_credit"] else "credit required")
    parts.append("derivatives allowed" if flags["allow_derivatives"] else "NO derivatives")
    parts.append(f"commercial: {', '.join(commercial) if commercial else 'none'}")
    flags["summary"] = " · ".join(parts)
    return flags


def pick_version(model, version_id):
    versions = (model or {}).get("modelVersions") or []
    if not versions:
        raise CivitaiError(
            f"'{model.get('name', 'that model')}' has no published version", LINK_REMEDIATION, 404
        )
    if version_id is None:
        return versions[0]  # Civitai lists newest first
    for version in versions:
        if int(version.get("id") or 0) == version_id:
            return version
    names = ", ".join(str(v.get("name") or v.get("id")) for v in versions[:6])
    raise CivitaiError(
        f"version {version_id} is not one of this model's versions",
        f"pick one of: {names}",
        404,
    )


def pick_archive(version):
    """The .zip of a version, with the size/hash the download is checked against."""
    files = (version or {}).get("files") or []
    archives = [f for f in files if str(f.get("name") or "").lower().endswith(ARCHIVE_SUFFIX)]
    if not archives:
        kinds = (
            ", ".join(sorted({str(f.get("name", "?")).rsplit(".", 1)[-1] for f in files})) or "?"
        )
        raise CivitaiError(
            f"version '{version.get('name', '?')}' ships no {ARCHIVE_SUFFIX} (found: {kinds})",
            "that version is not a wildcard archive — check the Files list on its Civitai page",
            404,
        )
    # the primary flag if there is one, else the first archive
    return next((f for f in archives if f.get("primary")), archives[0])


# -- the download -------------------------------------------------------------


def download_archive(entry, token, dest):
    """Stream one archive to `dest`, size-capped and SHA256-verified.

    Verification is not optional when Civitai supplies a hash: this file is
    about to be unpacked, and 'the bytes are what the catalogue says' is the
    cheapest guarantee available."""
    import hashlib

    from .lora import _open_download, _scrub_secrets

    url = str(entry.get("downloadUrl") or "")
    if not url.lower().startswith("https://"):
        raise CivitaiError("Civitai gave no usable download URL for that file", LINK_REMEDIATION)
    expected = str((entry.get("hashes") or {}).get("SHA256") or "").lower()
    digest = hashlib.sha256()
    written = 0
    try:
        with _open_download(url, token, timeout=DOWNLOAD_TIMEOUT) as resp, open(dest, "wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ARCHIVE_BYTES:
                    raise CivitaiError(
                        f"that archive is over {MAX_ARCHIVE_BYTES // 1024 // 1024} MB",
                        "wildcard packs are text — this is probably not one",
                    )
                digest.update(chunk)
                fh.write(chunk)
    except CivitaiError:
        raise
    except Exception as exc:
        raise CivitaiError(
            _scrub_secrets(f"the download failed ({type(exc).__name__}: {exc})"),
            KEY_REMEDIATION,
            502,
        ) from None
    if not written:
        raise CivitaiError("Civitai sent an empty file", KEY_REMEDIATION, 502)
    if expected and digest.hexdigest().lower() != expected:
        raise CivitaiError(
            "the downloaded archive does not match Civitai's SHA256",
            "try again — a truncated or intercepted download is the usual cause",
            502,
        )
    return written


# -- the import ---------------------------------------------------------------


def import_civitai_wildcards(lib, ref, *, overwrite=False, dry_run=True):
    """Resolve -> fetch -> plan. Answers the importer's plan shape with an
    extra `civitai` block (name, creator, version, licence), so the Composer's
    existing plan card renders it and only the credit line is new."""
    import tempfile
    from pathlib import Path

    from . import importers

    model_id, version_id = parse_model_ref(ref)
    token = _token(lib)
    if model_id is None:
        version = _get_json(VERSION_ENDPOINT.format(version_id=version_id), token, what="version")
        model_id = version.get("modelId")
        model = _get_json(MODEL_ENDPOINT.format(model_id=int(model_id)), token, what="model")
    else:
        model = _get_json(MODEL_ENDPOINT.format(model_id=model_id), token, what="model")
        version = pick_version(model, version_id)
    if model_type_of(model) != WILDCARD_TYPE:
        raise CivitaiError(
            f"'{model.get('name', 'that model')}' is a {model.get('type') or 'model'}, "
            "not a Wildcards pack",
            "LoRAs and checkpoints are downloaded from the LoRA blocks instead — this "
            "importer takes wildcard archives",
        )
    entry = pick_archive(version)
    licence = licence_of(model)
    with tempfile.TemporaryDirectory(prefix="mrln-civitai-") as tmp:
        archive = Path(tmp) / "wildcards.zip"
        download_archive(entry, token, archive)
        report = importers.import_wildcards(lib, str(archive), overwrite=overwrite, dry_run=dry_run)
    report["source"] = f"civitai:{model.get('id')}@{version.get('id')}"
    report["civitai"] = {
        "model": str(model.get("name") or ""),
        "model_id": model.get("id"),
        "version": str(version.get("name") or ""),
        "version_id": version.get("id"),
        "creator": str((model.get("creator") or {}).get("username") or ""),
        "url": f"https://civitai.com/models/{model.get('id')}",
        "file": str(entry.get("name") or ""),
        "licence": licence,
    }
    report["warnings"] = [
        f"licence for '{model.get('name')}' by "
        f"{(model.get('creator') or {}).get('username') or 'unknown'}: {licence['summary']}",
        *report.get("warnings", []),
    ]
    return report


@_guarded
def handle_import_civitai_wildcards(lib, payload):
    """POST {url, dry_run?, overwrite?} — import a Civitai Wildcards pack.

    `url` takes a civitai.com model link, a model-version link, an AIR urn or a
    bare `<model id>` / `<model id>@<version id>`. Defaults to dry_run=TRUE:
    this one reaches the network and writes files, so the default is the plan."""
    try:
        report = import_civitai_wildcards(
            lib,
            payload.get("url") or payload.get("id"),
            overwrite=bool(payload.get("overwrite")),
            dry_run=payload.get("dry_run", True) is not False,
        )
    except CivitaiError as exc:
        return exc.status, exc.body()
    except importers_error() as exc:  # the archive was fetched but is not a pack
        return exc.status, exc.body()
    report["fingerprint"] = lib.fingerprint()
    return 200, report


def importers_error():
    from .importers import ImporterError

    return ImporterError
