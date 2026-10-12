"""Download public source documents with integrity tracking.

Writes ``data/raw/<id>.pdf`` and a ``manifest.json`` (url, sha256, bytes, fetched_at) so every
indexed chunk can be traced to an exact file version, which is what an audit asks for.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import yaml

log = logging.getLogger(__name__)
MAX_BYTES = 150 * 1024 * 1024
_HEADERS = {"User-Agent": "insurance-rag-ingest/0.1 (research; contact: set-your-email)"}


def load_sources() -> list[dict[str, str]]:
    text = resources.files("insurance_rag.ingestion").joinpath("sources.yaml").read_text("utf-8")
    return list(yaml.safe_load(text)["sources"])


def download_all(dest: Path, *, force: bool = False) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path = dest / "manifest.json"
    manifest: dict[str, Any] = (
        json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    )
    with httpx.Client(follow_redirects=True, timeout=120, headers=_HEADERS) as client:
        for src in load_sources():
            target = dest / f"{src['id']}.pdf"
            if target.exists() and not force:
                log.info("skip (exists)", extra={"doc": src["id"]})
                continue
            try:
                manifest[src["id"]] = _fetch(client, src, target)
            except Exception as exc:  # one bad URL must not abort the whole run
                log.error("download failed", extra={"doc": src["id"], "error": str(exc)})
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def _fetch(client: httpx.Client, src: dict[str, str], target: Path) -> dict[str, Any]:
    sha = hashlib.sha256()
    size = 0
    tmp = target.with_suffix(".part")
    with client.stream("GET", src["url"]) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for block in resp.iter_bytes(65536):
                size += len(block)
                if size > MAX_BYTES:
                    raise ValueError("file exceeds size limit")
                sha.update(block)
                fh.write(block)
    with tmp.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            tmp.unlink()
            raise ValueError("response is not a PDF (the site may have returned an HTML page)")
    tmp.replace(target)
    log.info("downloaded", extra={"doc": src["id"], "bytes": size})
    return {
        "title": src["title"],
        "url": src["url"],
        "doc_type": src.get("doc_type", "unknown"),
        "sha256": sha.hexdigest(),
        "bytes": size,
        "fetched_at": datetime.now(UTC).isoformat(),
    }
