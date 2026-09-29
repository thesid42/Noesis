#!/usr/bin/env python3
"""Download and inspect the small, official AMI ES2002a demo sources.

The script deliberately handles only the five published low-resolution AVI
camera feeds, four headset WAVs, and continuous headset mix. It never requests
the much larger ``*_orig.avi`` files. Partial downloads stay as ``.part`` files
and are resumed with HTTP Range where the mirror supports it. A manifest entry
is written only after a complete file passes ffprobe and full decode checks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "ami" / "ES2002a"
BASE = "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/ES2002a"
VIDEO_FILES = [f"ES2002a.Closeup{i}.avi" for i in (1, 2, 3, 4)] + ["ES2002a.Corner.avi"]
AUDIO_FILES = [f"ES2002a.Headset-{i}.wav" for i in range(4)] + ["ES2002a.Mix-Headset.wav"]
FILES: dict[str, tuple[str, str]] = {
    **{name: ("video", f"{BASE}/video/{name}") for name in VIDEO_FILES},
    **{name: ("audio", f"{BASE}/audio/{name}") for name in AUDIO_FILES},
}


def _command(name: str, override: str | None = None) -> str:
    candidate = override or shutil.which(name)
    if not candidate:
        raise RuntimeError(f"{name} was not found on PATH; install FFmpeg or pass --{name}.")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _probe(path: Path, ffprobe: str, wanted_type: str | None = None) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_entries",
            "format=format_name,start_time,duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels,start_time,duration",
            "-of", "json", str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe failed for {path.name}: {result.stderr.strip()}")
    metadata = json.loads(result.stdout)
    streams = metadata.get("streams", [])
    wanted_type = wanted_type or ("video" if path.suffix.lower() in {".avi", ".mp4", ".mkv"} else "audio")
    if not any(stream.get("codec_type") == wanted_type for stream in streams):
        raise RuntimeError(f"{path.name} has no decodable {wanted_type} stream")
    duration = (metadata.get("format") or {}).get("duration")
    if not duration or float(duration) <= 0:
        raise RuntimeError(f"{path.name} has no positive stream duration")
    return metadata


def _decode_check(path: Path, ffmpeg: str) -> None:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        details = result.stderr.strip()[-1800:]
        raise RuntimeError(f"full decode check failed for {path.name}: {details}")


def _remote_size(url: str, timeout: int) -> int:
    headers = {"User-Agent": "Noesis-AMI-preparation/0.1 (hackathon demo)"}
    request = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=min(timeout, 20)) as response:
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > 0:
                return int(length)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        pass
    # A one-byte range gives a reliable total size from Content-Range even
    # when the server does not implement HEAD.
    request = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
    with urllib.request.urlopen(request, timeout=min(timeout, 20)) as response:
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", content_range.strip(), re.IGNORECASE)
        if match:
            return int(match.group(1))
        length = response.headers.get("Content-Length", "")
        if response.status == 200 and length.isdigit():
            return int(length)
    raise RuntimeError(f"the AMI mirror did not provide a response length for {url}")


def _range_block(url: str, start: int, end: int, total: int, timeout: int, retries: int) -> tuple[int, bytes]:
    headers = {
        "User-Agent": "Noesis-AMI-preparation/0.1 (hackathon demo)",
        "Range": f"bytes={start}-{end}",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 206:
                    raise RuntimeError(f"Range request returned HTTP {response.status} for bytes {start}-{end}")
                actual_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", actual_range.strip(), re.IGNORECASE)
                if not match or int(match.group(1)) != start or int(match.group(2)) != end:
                    raise RuntimeError(f"unexpected Content-Range {actual_range!r} for bytes {start}-{end}")
                if match.group(3) != "*" and int(match.group(3)) != total:
                    raise RuntimeError(f"mirror size changed during download (expected {total}, got {match.group(3)})")
                expected_length = end - start + 1
                content_length = response.headers.get("Content-Length", "")
                if content_length.isdigit() and int(content_length) != expected_length:
                    raise RuntimeError(
                        f"unexpected Content-Length {content_length} for bytes {start}-{end}"
                    )
                # Read exactly the range body. A one-byte over-read can block
                # forever on mirrors that keep the response connection open.
                payload = response.read(expected_length)
                if len(payload) != expected_length:
                    raise RuntimeError(f"short range response for bytes {start}-{end}: got {len(payload)} bytes")
                return start, payload
        except (urllib.error.URLError, TimeoutError, RuntimeError) as error:
            last_error = error
            if isinstance(error, RuntimeError) and "HTTP 5" not in str(error):
                raise
            if attempt + 1 < retries:
                time.sleep(min(0.25 * (2**attempt), 2.0))
    raise RuntimeError(f"range bytes {start}-{end} failed after {retries} attempts: {last_error}")


def _save_range_state(path: Path, total: int, chunk_size: int, completed: set[int]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {"total_bytes": total, "chunk_size": chunk_size, "completed": sorted(completed)}
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def _download(url: str, target: Path, timeout: int, chunk_size: int, workers: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    state_path = partial.with_name(partial.name + ".ranges.json")
    total = _remote_size(url, timeout)
    if total <= 0:
        raise RuntimeError(f"the AMI mirror reported an invalid size for {target.name}")
    chunk_count = (total + chunk_size - 1) // chunk_size
    existing = partial.stat().st_size if partial.exists() else 0
    completed: set[int] = set()
    saved_state = _safe_read_json(state_path)
    if (saved_state and saved_state.get("total_bytes") == total
            and saved_state.get("chunk_size") == chunk_size):
        completed = {int(index) for index in saved_state.get("completed", [])
                     if isinstance(index, int) and 0 <= index < chunk_count}
    else:
        # The earlier streaming downloader left a contiguous prefix without a
        # sidecar. Retain its complete 8 KiB blocks and redownload its tail.
        completed = set(range(min(chunk_count, existing // chunk_size))) if existing < total else set()

    mode = "r+b" if partial.exists() else "w+b"
    with partial.open(mode) as output:
        if existing > total:
            raise RuntimeError(f"partial {partial.name} exceeds the server's reported size")
        if existing != total:
            output.truncate(total)
            output.flush()
        if not saved_state or saved_state.get("total_bytes") != total or saved_state.get("chunk_size") != chunk_size:
            _save_range_state(state_path, total, chunk_size, completed)
        pending = [index for index in range(chunk_count) if index not in completed]
        if not pending:
            output.flush()
            os.fsync(output.fileno())
        else:
            batch_size = max(workers * 4, workers)
            done_since_save = 0
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    for batch_offset in range(0, len(pending), batch_size):
                        batch = pending[batch_offset:batch_offset + batch_size]
                        futures = {}
                        for index in batch:
                            start = index * chunk_size
                            end = min(total - 1, start + chunk_size - 1)
                            future = pool.submit(_range_block, url, start, end, total, timeout, 5)
                            futures[future] = index
                        batch_done = 0
                        for future in concurrent.futures.as_completed(futures):
                            index = futures[future]
                            start, payload = future.result()
                            output.seek(start)
                            output.write(payload)
                            completed.add(index)
                            batch_done += 1
                        output.flush()
                        os.fsync(output.fileno())
                        _save_range_state(state_path, total, chunk_size, completed)
                        done_since_save += batch_done
                        if total >= 1024 * 1024:
                            percent = int(len(completed) * 100 / chunk_count)
                            print(f"  ranges: {percent}% ({len(completed):,}/{chunk_count:,})", flush=True)
            except Exception:
                # Completed range blocks are flushed with their state before
                # each next batch, so rerunning safely resumes those blocks.
                output.flush()
                os.fsync(output.fileno())
                raise
    if len(completed) != chunk_count:
        raise RuntimeError(f"incomplete ranged download for {target.name}: {len(completed)} of {chunk_count} blocks")
    partial.replace(target)
    state_path.unlink(missing_ok=True)


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "session": "ES2002a", "files": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read manifest {path}: {error}") from error
    value.setdefault("files", {})
    value.setdefault("schema_version", 1)
    value.setdefault("session", "ES2002a")
    return value


def _save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _download_files(args: argparse.Namespace, selected: list[str]) -> tuple[dict[str, Any], Path]:
    root: Path = args.data_dir
    manifest_path = root / "download-manifest.json"
    manifest = _load_manifest(manifest_path)
    ffprobe = _command("ffprobe", args.ffprobe)
    ffmpeg = _command("ffmpeg", args.ffmpeg)
    for filename in selected:
        directory, url = FILES[filename]
        target = root / directory / filename
        print(f"[{filename}] {url}", flush=True)
        if target.exists():
            print(f"  existing file: {target.stat().st_size:,} bytes; validating", flush=True)
        else:
            _download(url, target, args.timeout, args.chunk_size, args.workers)
        probe = _probe(target, ffprobe)
        _decode_check(target, ffmpeg)
        entry = {
            "source_url": url,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "relative_path": target.relative_to(root).as_posix(),
            "bytes": target.stat().st_size,
            "sha256": _sha256(target),
            "ffprobe": probe,
            "decode_check": "ffmpeg full stream decode passed",
        }
        manifest["files"][filename] = entry
        _save_manifest(manifest_path, manifest)
        print(f"  verified: {entry['bytes']:,} bytes, sha256 {entry['sha256'][:16]}…, "
              f"duration {(probe.get('format') or {}).get('duration')}s", flush=True)
    return manifest, root


def _safe_read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _download_prefix(url: str, target: Path, requested_bytes: int, timeout: int,
                     chunk_size: int, workers: int) -> tuple[Path, dict[str, int | None]]:
    """Fetch only the initial byte window needed to decode a short excerpt."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size >= requested_bytes:
        # A local prefix is sufficient for the requested capped download; its
        # actual size and hash are recorded, while remote size remains unknown.
        return target, {"remote_bytes": None, "prefix_bytes": target.stat().st_size}
    total = _remote_size(url, timeout)
    prefix_bytes = min(total, requested_bytes)
    if target.is_file() and target.stat().st_size >= prefix_bytes:
        # Reuse a larger completed prefix when a later run asks for fewer
        # bytes. Never truncate or replace already verified source bytes.
        return target, {"remote_bytes": total, "prefix_bytes": target.stat().st_size}
    partial = target.with_name(target.name + ".part")
    state_path = partial.with_name(partial.name + ".ranges.json")
    if target.is_file() and not partial.exists():
        # A future larger cap extends this completed prefix. Copy it to the
        # resumable work file so its complete ranges remain reusable, while
        # keeping the previous final prefix intact until the extension ends.
        shutil.copyfile(target, partial)
    state = _safe_read_json(state_path)
    prefix_chunks = (prefix_bytes + chunk_size - 1) // chunk_size
    completed: set[int] = set()
    existing = partial.stat().st_size if partial.exists() else 0
    if (state and state.get("total_bytes") == total and state.get("chunk_size") == chunk_size):
        # If the requested prefix cap changes, retain every completed block
        # that still falls inside the smaller window. The part file can be
        # sparse, so deriving completion from its length would mislabel holes
        # as downloaded data.
        completed = {int(index) for index in state.get("completed", [])
                     if isinstance(index, int) and 0 <= index < prefix_chunks}
    else:
        # Preserve only the clearly contiguous prefix left by a previous run.
        completed = set(range(min(prefix_chunks, existing // chunk_size))) if existing < prefix_bytes else set()
    with partial.open("r+b" if partial.exists() else "w+b") as output:
        output.truncate(prefix_bytes)
        if not state or state.get("total_bytes") != total or state.get("prefix_bytes") != prefix_bytes or state.get("chunk_size") != chunk_size:
            payload = {"total_bytes": total, "prefix_bytes": prefix_bytes,
                       "chunk_size": chunk_size, "completed": sorted(completed)}
            temporary = state_path.with_suffix(state_path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            temporary.replace(state_path)
        pending = [index for index in range(prefix_chunks) if index not in completed]
        batch_size = max(workers * 2, workers)
        retry_round = 0
        while pending:
            failed: list[int] = []
            total_batches = (len(pending) + batch_size - 1) // batch_size
            for batch_offset in range(0, len(pending), batch_size):
                batch = pending[batch_offset:batch_offset + batch_size]
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {}
                    for index in batch:
                        start = index * chunk_size
                        end = min(prefix_bytes - 1, start + chunk_size - 1)
                        futures[pool.submit(_range_block, url, start, end, total, timeout, 2)] = index
                    for future in concurrent.futures.as_completed(futures):
                        index = futures[future]
                        try:
                            start, block = future.result()
                        except Exception as error:
                            failed.append(index)
                            print(f"  range {index + 1}/{prefix_chunks} will retry: {error}", flush=True)
                            continue
                        output.seek(start)
                        output.write(block)
                        completed.add(index)
                output.flush()
                os.fsync(output.fileno())
                payload = {"total_bytes": total, "prefix_bytes": prefix_bytes,
                           "chunk_size": chunk_size, "completed": sorted(completed)}
                temporary = state_path.with_suffix(state_path.suffix + ".tmp")
                temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
                temporary.replace(state_path)
                print(f"  prefix ranges: {len(completed):,}/{prefix_chunks:,}", flush=True)
            if not failed:
                pending = []
                break
            retry_round += 1
            if retry_round >= 8:
                raise RuntimeError(
                    f"{len(failed)} ranges remain after {retry_round} durable retry rounds; "
                    "successful ranges were saved and rerunning resumes them"
                )
            pending = sorted(set(failed))
            # Back off between retry rounds and shrink each work batch so a
            # temporarily overloaded mirror can recover without losing work.
            time.sleep(min(0.5 * retry_round, 3.0))
            batch_size = max(workers, batch_size // 2)
    if len(completed) != prefix_chunks:
        raise RuntimeError(f"incomplete prefix for {target.name}: {len(completed)} of {prefix_chunks} blocks")
    partial.replace(target)
    state_path.unlink(missing_ok=True)
    return target, {"remote_bytes": total, "prefix_bytes": prefix_bytes}


def _prepare_clip(args: argparse.Namespace, manifest: dict[str, Any], root: Path, selected: list[str],
                  source_overrides: dict[str, Path] | None = None, input_kind: str = "complete",
                  source_urls: dict[str, str] | None = None,
                  source_formats: dict[str, str] | None = None,
                  audio_gain_db: float = 0.0) -> None:
    start = args.clip_start
    duration = args.clip_duration
    if start < 0 or duration <= 0:
        raise RuntimeError("--clip-start must be nonnegative and --clip-duration must be positive")
    ffmpeg = _command("ffmpeg", args.ffmpeg)
    ffprobe = _command("ffprobe", args.ffprobe)
    output_root = root / "prepared" / f"clip-{start:g}-{duration:g}"
    output_root.mkdir(parents=True, exist_ok=True)
    records: dict[str, Any] = {}
    for filename in selected:
        source_overrides = source_overrides or {}
        source_urls = source_urls or {}
        source_formats = source_formats or {}
        source = source_overrides.get(filename)
        source_entry = manifest["files"].get(filename)
        if source is None and source_entry is None:
            raise RuntimeError(f"{filename} is not verified in the manifest")
        if source is None:
            source = root / Path(source_entry["relative_path"])
        source_sha = source_entry["sha256"] if source_entry else _sha256(source)
        media_type = "video" if filename.endswith(".avi") else "audio"
        source_probe = _probe(source, ffprobe, media_type)
        source_stream = next(stream for stream in source_probe.get("streams", [])
                             if stream.get("codec_type") == media_type)
        source_url = source_urls.get(filename, FILES[filename][1])
        output_name = Path(filename).stem + (".mp4" if filename.endswith(".avi") else ".wav")
        output = output_root / output_name
        if filename.endswith(".avi"):
            command = [
                ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", str(start), "-i", str(source),
                "-t", str(duration), "-map", "0:v:0", "-an", "-vf", "scale='min(1280,iw)':-2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(output),
            ]
        else:
            gain_args = ["-af", f"volume={audio_gain_db:g}dB"] if audio_gain_db else []
            command = [
                ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", str(start), "-i", str(source),
                "-t", str(duration), "-map", "0:a:0", "-vn", *gain_args,
                "-c:a", "pcm_s16le", str(output),
            ]
        result = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            output.unlink(missing_ok=True)
            raise RuntimeError(f"clip preparation failed for {filename}: {result.stderr.strip()[-1800:]}")
        probe = _probe(output, ffprobe, media_type)
        _decode_check(output, ffmpeg)
        output_duration = float((probe.get("format") or {}).get("duration", 0.0))
        if output_duration + 0.25 < duration:
            output.unlink(missing_ok=True)
            raise RuntimeError(
                f"{filename} yielded only {output_duration:.2f}s of the requested {duration:.2f}s clip; "
                "increase the corresponding prefix-byte limit and retry"
            )
        records[filename] = {
            "source_sha256": source_sha,
            "source_kind": input_kind,
            "source_url": source_url,
            "source_format": source_formats.get(filename, Path(source).suffix.lstrip(".")),
            "source_codec": source_stream.get("codec_name"),
            "fixed_gain_db": audio_gain_db if media_type == "audio" else None,
            "relative_output_path": output.relative_to(root).as_posix(),
            "bytes": output.stat().st_size,
            "sha256": _sha256(output),
            "derivative": {
                "container": output.suffix.lstrip("."),
                "codec": next(stream.get("codec_name") for stream in probe.get("streams", [])
                              if stream.get("codec_type") == media_type),
                "duration_s": output_duration,
                "derived_from": source_url,
                "fixed_gain_db": audio_gain_db if media_type == "audio" else None,
            },
            "ffmpeg_arguments": command[1:],
            "ffprobe": probe,
        }
        print(f"[prepared] {output.relative_to(root)} ({output.stat().st_size:,} bytes)", flush=True)
    prepared_manifest = {
        "schema_version": 1,
        "session": "ES2002a",
        "source_start_s": start,
        "requested_duration_s": duration,
        "audio_gain_db": audio_gain_db,
        "timestamp_note": "All outputs use the same source start and requested duration; verify A/V alignment before evaluation.",
        "files": records,
    }
    (output_root / "manifest.json").write_text(json.dumps(prepared_manifest, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA, help="raw ES2002a data directory")
    parser.add_argument("--only", action="append", choices=sorted(FILES), help="download/check one exact file (repeatable)")
    parser.add_argument("--list", action="store_true", help="list official HTTPS URLs without downloading")
    parser.add_argument("--prepare-clip", action="store_true", help="also transcode the selected excerpt after verification")
    parser.add_argument("--clip-start", type=float, default=0.0, help="excerpt start in seconds (default: 0)")
    parser.add_argument("--clip-duration", type=float, default=150.0, help="excerpt duration in seconds (default: 150)")
    parser.add_argument("--preview-seconds", type=float, help="download bounded source prefixes and prepare a 20–30s AMI preview")
    parser.add_argument("--preview-video-format", choices=("avi", "rm"), default="avi",
                        help="official video source format for a bounded preview (default: avi)")
    parser.add_argument("--preview-audio-format", choices=("wav", "rm"), default="wav",
                        help="official audio source format for a bounded preview (default: wav)")
    parser.add_argument("--prefer-cached-prefixes", action="store_true",
                        help="reuse an existing complete prefix in the canonical AVI/WAV format when available")
    parser.add_argument("--video-prefix-bytes", type=int,
                        help="maximum prefix bytes per video file (defaults: 4 MiB AVI, 256 KiB RM; cap 16 MiB)")
    parser.add_argument("--audio-prefix-bytes", type=int,
                        help="maximum prefix bytes per audio file (defaults: 2 MiB WAV, 128 KiB RM; cap 4 MiB)")
    parser.add_argument("--preview-audio-gain-db", type=float, default=0.0,
                        help="apply one fixed gain to every prepared audio source (default: 0 dB)")
    parser.add_argument("--chunk-size", type=int, default=None, help="HTTP Range block size (default: 8 KiB for bounded previews, 1 MiB for full files)")
    parser.add_argument("--workers", type=int, default=8, help="concurrent HTTP range requests (default: 8)")
    parser.add_argument("--timeout", type=int, default=90, help="per-response timeout in seconds")
    parser.add_argument("--ffmpeg", help="path to ffmpeg")
    parser.add_argument("--ffprobe", help="path to ffprobe")
    args = parser.parse_args(argv)
    if args.chunk_size is None:
        args.chunk_size = 8192 if args.preview_seconds is not None else 1024 * 1024
    selected = args.only or list(FILES)
    if args.list:
        for name in selected:
            print(f"{name}\t{FILES[name][1]}")
        return 0
    try:
        if args.workers < 1 or args.workers > 16 or args.chunk_size < 1024 or args.chunk_size > 1024 * 1024:
            raise RuntimeError("--workers must be 1–16 and --chunk-size must be between 1 KiB and 1 MiB")
        if args.preview_seconds is not None:
            if not 20 <= args.preview_seconds <= 30:
                raise RuntimeError("--preview-seconds is bounded to 20–30 seconds")
            if args.clip_start != 0:
                raise RuntimeError("prefix previews start at source time zero; full-download --prepare-clip supports other times")
            if args.video_prefix_bytes is None:
                args.video_prefix_bytes = 256 * 1024 if args.preview_video_format == "rm" else 4 * 1024 * 1024
            if args.audio_prefix_bytes is None:
                args.audio_prefix_bytes = 128 * 1024 if args.preview_audio_format == "rm" else 2 * 1024 * 1024
            if args.video_prefix_bytes < 1 or args.video_prefix_bytes > 16 * 1024 * 1024:
                raise RuntimeError("--video-prefix-bytes must be between 1 byte and 16 MiB")
            if args.audio_prefix_bytes < 1 or args.audio_prefix_bytes > 4 * 1024 * 1024:
                raise RuntimeError("--audio-prefix-bytes must be between 1 byte and 4 MiB")
            if not math.isfinite(args.preview_audio_gain_db) or not -60 <= args.preview_audio_gain_db <= 60:
                raise RuntimeError("--preview-audio-gain-db must be finite and between -60 and 60")
            if set(selected) != set(FILES):
                raise RuntimeError("a playable preview requires all five videos and all five audio files; omit --only")
            root = args.data_dir
            source_overrides: dict[str, Path] = {}
            source_urls: dict[str, str] = {}
            source_formats: dict[str, str] = {}
            prefix_report: dict[str, Any] = {}
            for filename in selected:
                directory, _ = FILES[filename]
                source_format = args.preview_video_format if directory == "video" else args.preview_audio_format
                if args.prefer_cached_prefixes:
                    requested_path = root / "prefix" / directory / f"{Path(filename).stem}.{source_format}"
                    canonical_format = Path(filename).suffix.lstrip(".")
                    cached_path = root / "prefix" / directory / f"{Path(filename).stem}.{canonical_format}"
                    if requested_path.is_file():
                        pass
                    elif cached_path.is_file():
                        source_format = canonical_format
                source_name = f"{Path(filename).stem}.{source_format}"
                url = f"{BASE}/{directory}/{source_name}"
                limit = args.video_prefix_bytes if directory == "video" else args.audio_prefix_bytes
                prefix_path = root / "prefix" / directory / source_name
                _, sizes = _download_prefix(url, prefix_path, limit, args.timeout, args.chunk_size, args.workers)
                source_overrides[filename] = prefix_path
                source_urls[filename] = url
                source_formats[filename] = source_format
                prefix_report[filename] = {
                    "source_url": url,
                    "source_format": source_format,
                    "relative_path": prefix_path.relative_to(root).as_posix(),
                    "bytes": sizes["prefix_bytes"],
                    "remote_bytes": sizes["remote_bytes"],
                    "sha256": _sha256(prefix_path),
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                }
            preview_manifest = {"schema_version": 1, "session": "ES2002a", "files": {}}
            args.clip_duration = args.preview_seconds
            _prepare_clip(args, preview_manifest, root, selected, source_overrides,
                          input_kind="bounded source prefix; decoded to the requested short excerpt",
                          source_urls=source_urls, source_formats=source_formats,
                          audio_gain_db=args.preview_audio_gain_db)
            out_dir = root / "prepared" / f"clip-{args.clip_start:g}-{args.preview_seconds:g}"
            manifest_path = out_dir / "manifest.json"
            prepared = _load_manifest(manifest_path)
            prepared["input_kind"] = "bounded source prefixes, then locally trimmed/transcoded"
            prepared["prefix_sources"] = prefix_report
            prepared["requested_duration_s"] = args.preview_seconds
            _save_manifest(manifest_path, prepared)
            args.clip_duration = args.preview_seconds
        else:
            manifest, root = _download_files(args, selected)
            if args.prepare_clip:
                _prepare_clip(args, manifest, root, selected)
    except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.preview_seconds is not None:
        print(f"\nPrepared bounded AMI preview under: {root / 'prepared' / f'clip-{args.clip_start:g}-{args.preview_seconds:g}'}")
    else:
        print(f"\nVerified manifest: {root / 'download-manifest.json'}")
    if args.prepare_clip and args.preview_seconds is None:
        print(f"Prepared selected excerpt under: {root / 'prepared' / f'clip-{args.clip_start:g}-{args.clip_duration:g}'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
