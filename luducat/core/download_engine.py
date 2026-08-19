# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""HTTP download engine — transfer primitives for the Luducat Downloader.

Qt-free by design. Uses requests + threading. Progress via callbacks.
Derived from mget v0.25 transfer primitives, stripped of Rich/crawler/scanner.
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests as _requests_module

logger = logging.getLogger(__name__)

# HTTP status codes that should not be retried.
PERMANENT_HTTP_ERRORS = {401, 403, 404, 410, 451}

# S3/Spaces presigned URLs bind the HTTP verb into the signature,
# so a HEAD on a GET-signed URL returns 403.
_S3_SIGNATURE_PARAMS = frozenset({"X-Amz-Signature", "Signature", "AWSAccessKeyId"})


def _is_signed_url(url: str) -> bool:
    qs = parse_qs(urlparse(url).query)
    return bool(_S3_SIGNATURE_PARAMS & qs.keys())


@dataclass(slots=True)
class DownloadResult:
    """Result of a download_file() call."""

    ok: bool
    reason: str                                  # empty on success
    http_status: int = 0
    size: int = 0
    dest_path: Optional[Path] = None
    checksum_sha256: str = ""                    # always computed
    checksum_store: str = ""                     # prefixed, e.g. "md5:abc..."
    remote_mtime: Optional[float] = None         # epoch seconds from Last-Modified
    content_type: str = ""
    chunk_state: Optional[dict] = None           # per-chunk progress for resume


def remote_mtime(response) -> Optional[float]:
    """Extract modification time from HTTP Last-Modified header.

    Returns epoch timestamp or None if header is missing/malformed.
    """
    lm = response.headers.get("Last-Modified")
    if lm:
        try:
            return parsedate_to_datetime(lm).timestamp()
        except Exception:
            pass
    return None


def stream_download(
    url: str,
    tmp_path: Path,
    session: Optional[_requests_module.Session],
    resume_offset: int,
    total_size: int,
    progress_callback,
    cancel_event: Optional[threading.Event],
    extra_headers: Optional[dict],
    timeout: float,
) -> None:
    """Single-connection download with Range resume.

    Writes to tmp_path. Raises IOError on HTTP errors.
    progress_callback signature: (bytes_written: int, total: int)
    """
    if session is None:
        session = _requests_module.Session()

    headers = dict(extra_headers or {})
    if resume_offset:
        headers["Range"] = f"bytes={resume_offset}-"

    r = session.get(url, headers=headers, stream=True,
                    timeout=timeout, allow_redirects=True)

    if r.status_code == 416:
        return  # already complete

    if r.status_code not in (200, 206):
        raise IOError(f"HTTP {r.status_code}")

    if total_size == 0:
        cl = int(r.headers.get("Content-Length", 0) or 0)
        total_size = cl + (resume_offset if r.status_code == 206 else 0)

    mode = "ab" if (resume_offset and r.status_code == 206) else "wb"
    written = resume_offset if mode == "ab" else 0

    with open(tmp_path, mode) as fh:
        for chunk in r.iter_content(65536):
            if cancel_event and cancel_event.is_set():
                break
            if chunk:
                fh.write(chunk)
                written += len(chunk)
                if progress_callback:
                    progress_callback(written, total_size)


def _fetch_range(
    url: str,
    tmp_path: Path,
    session: _requests_module.Session,
    start: int,
    end: int,
    progress_callback,
    written_ref: list,
    lock: threading.Lock,
    cancel_event: Optional[threading.Event],
    extra_headers: Optional[dict],
    timeout: float,
    chunk_index: int = 0,
    resume_offset: int = 0,
    per_chunk_written: Optional[list] = None,
) -> None:
    """Download one byte range and write it at the correct offset."""
    if cancel_event and cancel_event.is_set():
        return

    actual_start = start + resume_offset
    if actual_start > end:
        return

    headers = dict(extra_headers or {})
    headers["Range"] = f"bytes={actual_start}-{end}"
    r = session.get(url, headers=headers, stream=True,
                    timeout=timeout, allow_redirects=True)

    if r.status_code == 416:
        return

    if r.status_code != 206:
        raise IOError(f"Chunk {start}-{end}: expected 206, got {r.status_code}")

    with open(tmp_path, "r+b") as fh:
        fh.seek(actual_start)
        for chunk in r.iter_content(65536):
            if cancel_event and cancel_event.is_set():
                return
            if chunk:
                fh.write(chunk)
                n = len(chunk)
                with lock:
                    written_ref[0] += n
                    if per_chunk_written is not None:
                        per_chunk_written[chunk_index] += n
                    current = written_ref[0]
                if progress_callback:
                    progress_callback(current, 0)

    chunk_bytes = end - start + 1
    if per_chunk_written is not None:
        logger.debug("Chunk %d done: %d/%d bytes", chunk_index,
                      per_chunk_written[chunk_index], chunk_bytes)


def chunked_download(
    url: str,
    tmp_path: Path,
    session: Optional[_requests_module.Session],
    total_size: int,
    num_connections: int,
    progress_callback,
    cancel_event: Optional[threading.Event],
    extra_headers: Optional[dict],
    timeout: float,
    chunk_state: Optional[dict] = None,
) -> tuple[int, Optional[dict]]:
    """Parallel range download: pre-allocate file, fill N ranges concurrently.

    Returns (actual_bytes_written, final_chunk_state).
    chunk_state is None on full completion, populated on partial (cancel/pause).
    """
    if session is None:
        session = _requests_module.Session()

    resuming = False
    if (chunk_state
            and chunk_state.get("total_size") == total_size
            and tmp_path.exists()
            and tmp_path.stat().st_size == total_size):
        chunks = chunk_state["chunks"]
        ranges = [(c["start"], c["end"]) for c in chunks]
        per_chunk_written = [c["written"] for c in chunks]
        written = [sum(per_chunk_written)]
        num_connections = len(chunks)
        resuming = True
        done_count = sum(1 for c in chunks if c.get("done"))
        logger.debug("Resuming chunked download: %d/%d chunks done, %d bytes already written",
                      done_count, len(chunks), written[0])
    else:
        if chunk_state:
            logger.debug("Chunk state invalid (size mismatch or missing file), starting fresh")
        chunk_state = None
        with open(tmp_path, "wb") as fh:
            fh.seek(total_size - 1)
            fh.write(b"\0")

        chunk_size = total_size // num_connections
        ranges = [
            (i * chunk_size, (i * chunk_size + chunk_size - 1) if i < num_connections - 1 else total_size - 1)
            for i in range(num_connections)
        ]
        per_chunk_written = [0] * num_connections
        written = [0]

    lock = threading.Lock()

    def total_progress(written_bytes, _per_chunk_total):
        if progress_callback:
            progress_callback(written_bytes, total_size)

    with ThreadPoolExecutor(max_workers=num_connections) as ex:
        futs = []
        for i, (s, e) in enumerate(ranges):
            if resuming and chunk_state["chunks"][i].get("done"):
                continue
            resume_offset = per_chunk_written[i] if resuming else 0
            futs.append(ex.submit(
                _fetch_range, url, tmp_path, session,
                s, e, total_progress, written, lock,
                cancel_event, extra_headers, timeout,
                chunk_index=i,
                resume_offset=resume_offset,
                per_chunk_written=per_chunk_written,
            ))
        for fut in as_completed(futs):
            fut.result()

    with lock:
        final_written = written[0]
        snapshot = list(per_chunk_written)

    if final_written >= total_size:
        return final_written, None

    final_state = {
        "version": 1,
        "total_size": total_size,
        "num_connections": num_connections,
        "chunks": [
            {
                "index": i,
                "start": ranges[i][0],
                "end": ranges[i][1],
                "written": snapshot[i],
                "done": snapshot[i] >= (ranges[i][1] - ranges[i][0] + 1),
            }
            for i in range(num_connections)
        ],
    }
    return final_written, final_state


def _compute_checksums(
    file_path: Path,
    expected_checksum: Optional[str],
) -> tuple[str, str]:
    """Compute SHA-256 (always) and optionally verify a store-provided checksum.

    Returns (sha256_hex, store_checksum_prefixed).
    store_checksum_prefixed is empty string if no expected_checksum.
    Raises IOError on mismatch.
    """
    sha256_ctx = hashlib.sha256()
    store_ctx = None
    store_prefix = ""

    if expected_checksum and ":" in expected_checksum:
        store_prefix, expected_hex = expected_checksum.split(":", 1)
        algo = store_prefix.lower()
        if algo == "md5":
            store_ctx = hashlib.md5(usedforsecurity=False)
        elif algo == "sha256":
            store_ctx = hashlib.sha256()
        elif algo == "sha1":
            store_ctx = hashlib.sha1(usedforsecurity=False)
        else:
            logger.warning("Unknown checksum algorithm %r, skipping verification", algo)

    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sha256_ctx.update(chunk)
            if store_ctx:
                store_ctx.update(chunk)

    sha256_hex = sha256_ctx.hexdigest()
    store_result = ""

    if store_ctx and expected_checksum:
        computed = store_ctx.hexdigest()
        _, expected_hex = expected_checksum.split(":", 1)
        store_result = f"{store_prefix}:{computed}"
        if computed.lower() != expected_hex.lower():
            raise IOError(
                f"Checksum mismatch: expected {expected_checksum}, "
                f"got {store_prefix}:{computed}"
            )

    return sha256_hex, store_result


def download_file(
    url: str,
    dest_path: Path,
    session: Optional[_requests_module.Session] = None,
    *,
    cancel_event: Optional[threading.Event] = None,
    progress_callback=None,
    num_connections: int = 1,
    chunk_threshold: int = 50 * 1024 * 1024,
    retries: int = 10,
    extra_headers: Optional[dict] = None,
    timeout: float = 60,
    expected_checksum: Optional[str] = None,
    listing_mtime: Optional[float] = None,
    chunk_state: Optional[dict] = None,
) -> DownloadResult:
    """Download a single URL. Returns DownloadResult.

    Signed URLs (S3/Spaces) skip the HEAD probe and fall back to
    Content-Length from the streaming GET response.
    """
    if session is None:
        session = _requests_module.Session()

    dest_path = Path(dest_path)
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DownloadResult(False, f"path error: {exc}", 0, 0, dest_path)

    tmp = dest_path.with_name(dest_path.name + ".luducat-tmp")
    last_exc = ""
    last_http = 0
    hdrs = dict(extra_headers or {})

    for attempt in range(retries):
        if cancel_event and cancel_event.is_set():
            return DownloadResult(False, "cancelled", 0, 0, dest_path)

        try:
            # Rate-limit retries (skip first attempt)
            if attempt > 0:
                backoff = min(2 ** attempt, 30)
                if cancel_event:
                    cancel_event.wait(backoff)
                    if cancel_event.is_set():
                        return DownloadResult(False, "cancelled", 0, 0, dest_path)
                else:
                    time.sleep(backoff)

            signed = _is_signed_url(url)
            if signed:
                total = 0
                accepts_range = False
                mt = None
                content_type = ""
                last_http = 0
            else:
                head = session.head(url, headers=hdrs, allow_redirects=True, timeout=10)
                last_http = head.status_code

                if last_http in PERMANENT_HTTP_ERRORS:
                    return DownloadResult(False, f"HTTP {last_http}", last_http, 0, dest_path)

                total = int(head.headers.get("Content-Length", 0) or 0)
                content_enc = head.headers.get("Content-Encoding", "").lower()
                if content_enc in ("gzip", "br", "deflate", "zstd"):
                    total = 0
                accepts_range = head.headers.get("Accept-Ranges", "none").lower() == "bytes"
                mt = remote_mtime(head)
                content_type = head.headers.get("Content-Type", "")

            use_chunks = (
                num_connections > 1
                and total >= chunk_threshold
                and accepts_range
            )

            if use_chunks:
                if chunk_state and tmp.exists() and tmp.stat().st_size == total:
                    pass  # preserve temp file for resume
                elif tmp.exists():
                    tmp.unlink()
                    chunk_state = None

                actual, chunk_state = chunked_download(
                    url, tmp, session, total, num_connections,
                    progress_callback, cancel_event, hdrs, timeout,
                    chunk_state=chunk_state,
                )
                if cancel_event and cancel_event.is_set():
                    return DownloadResult(
                        False, "cancelled", 0, actual, dest_path,
                        chunk_state=chunk_state,
                    )
                if total > 0 and actual != total:
                    raise IOError(f"size mismatch: expected {total}, written {actual}")
                got = actual
                chunk_state = None
            else:
                resume = tmp.stat().st_size if tmp.exists() else 0
                stream_download(
                    url, tmp, session, resume, total,
                    progress_callback, cancel_event, hdrs, timeout,
                )
                got = tmp.stat().st_size
                if total > 0 and got != total:
                    raise IOError(f"size mismatch: expected {total}, got {got}")

            # Dual checksum (sequential read pass)
            sha256_hex, store_checksum = _compute_checksums(tmp, expected_checksum)

            # Determine timestamp
            stamp = listing_mtime if listing_mtime is not None else mt

            return DownloadResult(
                ok=True,
                reason="",
                http_status=last_http,
                size=got,
                dest_path=tmp,
                checksum_sha256=sha256_hex,
                checksum_store=store_checksum,
                remote_mtime=stamp,
                content_type=content_type,
            )

        except IOError as exc:
            last_exc = str(exc)
            if "checksum mismatch" in last_exc.lower():
                # Don't retry checksum failures — the data is corrupt
                return DownloadResult(False, last_exc, last_http, 0, dest_path)
        except Exception as exc:
            last_exc = str(exc)
            logger.debug("download_file attempt %d/%d failed: %s", attempt + 1, retries, last_exc)

    return DownloadResult(False, last_exc, last_http, 0, dest_path)
