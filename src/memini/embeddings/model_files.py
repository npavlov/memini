"""The files of a local model: where they live, the digest that pins them, and fetching them.

A local model is two files from a Hugging Face model repository: the ONNX model (by default
``onnx/model.onnx``) and ``tokenizer.json``. Their digest, ``embedding_digest``, pins them:

    sha256:<hex>, hex = memini.ids.hash_fields(
        "memini.model-files.v1", path₁, sha256(file₁), path₂, sha256(file₂), ...)

over the files in order of their path in the repository, each path in UTF-8 followed by the 32
bytes of the file's SHA-256. The paths are part of it, so the same bytes under another name, or a
quantized variant of the same model, are another digest.

:func:`ensure_model_files` fails closed: files whose digest differs are never loaded. Missing files
are fetched from ``{hub}/{model}/resolve/{revision}/{path}``, where the revision is a commit (a
branch or tag could move), into a staging directory, and moved into place only when their digest
matches. Each pinned digest has a directory of its own (:func:`model_files_dir`), so a new pin never
overwrites files another process still uses.
"""

import hashlib
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import anyio
import anyio.to_thread
import httpx2

from memini import __version__
from memini.embeddings.base import EmbeddingError
from memini.ids import hash_fields

__all__ = [
    "HUB_URL",
    "MODEL_FILES_DOMAIN",
    "TOKENIZER_FILE",
    "digest_of_files",
    "ensure_model_files",
    "fetch_model_files",
    "files_digest",
    "hash_file",
    "model_files_dir",
]

HUB_URL: Final = "https://huggingface.co"
"""Where missing model files are fetched from."""

TOKENIZER_FILE: Final = "tokenizer.json"
"""The tokenizer of a model, in the format of the Hugging Face ``tokenizers`` library."""

MODEL_FILES_DOMAIN: Final = "memini.model-files.v1"
"""Domain tag of the digest of model files."""

_CHUNK: Final = 1 << 20

log = logging.getLogger(__name__)


def model_files_dir(root: Path, model: str, digest: str) -> Path:
    """The directory of the files of ``model`` pinned by ``digest``, under ``root``.

    ``org/name`` and ``sha256:0123...`` give ``root/org--name/sha256-0123456789abcdef``.
    """
    return root / model.replace("/", "--") / digest.replace(":", "-")[:23]


def files_digest(hashes: Mapping[str, str]) -> str:
    """The digest of model files, from the SHA-256 (hex) of each file by its path."""
    fields: list[bytes] = []
    for name in sorted(hashes):
        fields += [name.encode(), bytes.fromhex(hashes[name])]
    return "sha256:" + hash_fields(MODEL_FILES_DOMAIN, *fields)


def hash_file(path: Path) -> str:
    """SHA-256 of the file at ``path``, as 64 lowercase hex digits. Blocking."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


async def digest_of_files(directory: Path, names: Sequence[str]) -> str:
    """:func:`files_digest` of the files ``names`` in ``directory``, hashed in a worker thread."""
    hashes: dict[str, str] = {}
    for name in names:
        hashes[name] = await anyio.to_thread.run_sync(hash_file, directory / name)
    return files_digest(hashes)


async def ensure_model_files(
    *,
    directory: Path,
    model: str,
    names: Sequence[str],
    digest: str,
    revision: str | None,
    hub_url: str = HUB_URL,
    timeout: float = 60.0,
    transport: httpx2.AsyncBaseTransport | None = None,
    settings: tuple[str, str] = ("embedding_digest", "embedding_revision"),
) -> None:
    """Make sure ``directory`` holds the files ``names`` of ``model``, matching ``digest``.

    Files that are all there are verified. Otherwise they are fetched at ``revision``.
    ``settings`` names the settings of the digest and the revision, for messages.

    Raises:
        EmbeddingError: the files differ from ``digest``; files are missing and there is no
            revision to fetch them at; or fetching failed. Nothing that failed is left in place.
    """
    missing = [name for name in names if not (directory / name).is_file()]
    if not missing:
        try:
            actual = await digest_of_files(directory, names)
        except OSError as error:
            raise EmbeddingError(
                f"cannot read the files of model {model} in {directory}: {_reason(error)}"
            ) from None
        if actual != digest:
            raise EmbeddingError(
                f"the files of model {model} in {directory} have digest {actual}, but"
                f" {settings[0]} is {digest}; refusing to load them. Delete {directory} to fetch"
                " them again, or pin the digest of files you trust"
            )
        return
    if revision is None:
        raise EmbeddingError(
            f"the files of model {model} are not in {directory} (missing: {', '.join(missing)});"
            f" set MEMINI_{settings[1].upper()} to fetch them, or put {', '.join(names)} there"
        )
    await fetch_model_files(
        directory=directory,
        model=model,
        names=names,
        digest=digest,
        revision=revision,
        hub_url=hub_url,
        timeout=timeout,
        transport=transport,
        digest_setting=settings[0],
    )


async def fetch_model_files(
    *,
    directory: Path,
    model: str,
    names: Sequence[str],
    digest: str,
    revision: str,
    hub_url: str = HUB_URL,
    timeout: float = 60.0,
    transport: httpx2.AsyncBaseTransport | None = None,
    digest_setting: str = "embedding_digest",
) -> None:
    """Download ``names`` of ``model`` at ``revision`` into ``directory`` if they match ``digest``
    (the value of the setting ``digest_setting``, named in messages).

    Raises:
        EmbeddingError: a download failed, or the files do not match ``digest``. ``directory`` is
            left as it was.
    """
    try:
        directory.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".fetch-", dir=directory.parent))
    except OSError as error:
        raise EmbeddingError(f"cannot create {directory}: {_reason(error)}") from None
    base = f"{hub_url.rstrip('/')}/{model}/resolve/{revision}"
    log.info("fetching model %s at revision %s from %s", model, revision, hub_url)
    try:
        hashes: dict[str, str] = {}
        async with httpx2.AsyncClient(
            headers={"User-Agent": f"memini/{__version__}"},
            timeout=httpx2.Timeout(timeout),
            follow_redirects=True,  # the hub answers with a redirect to its file storage
            transport=transport,
        ) as client:
            for name in names:
                what = f"{name} of model {model} at revision {revision}"
                hashes[name] = await _download(client, f"{base}/{name}", staging / name, what)
        actual = files_digest(hashes)
        if actual != digest:
            raise EmbeddingError(
                f"the files of model {model} at revision {revision} have digest {actual}, but"
                f" {digest_setting} is {digest}; refusing to use them"
            )
        for name in names:
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / name, target)
    except OSError as error:
        raise EmbeddingError(f"cannot store the files of model {model}: {_reason(error)}") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    log.info("model %s stored in %s", model, directory)


async def _download(client: httpx2.AsyncClient, url: str, target: Path, what: str) -> str:
    """Stream ``url`` into ``target``; return the SHA-256 of what was written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        async with client.stream("GET", url) as response:
            if not response.is_success:
                raise EmbeddingError(f"cannot fetch {what}: HTTP {response.status_code}")
            async with await anyio.open_file(target, "wb") as file:
                async for chunk in response.aiter_bytes(_CHUNK):
                    digest.update(chunk)
                    await file.write(chunk)
    except httpx2.TransportError as error:
        raise EmbeddingError(f"cannot fetch {what}: {type(error).__name__}") from None
    return digest.hexdigest()


def _reason(error: OSError) -> str:
    return error.strerror or type(error).__name__
