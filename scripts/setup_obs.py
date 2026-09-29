"""Fetch the official portable OBS release into this workspace, verifying SHA-256."""

from __future__ import annotations

import hashlib
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "32.2.2"
URL = f"https://github.com/obsproject/obs-studio/releases/download/{VERSION}/OBS-Studio-{VERSION}-Windows-x64.zip"
SHA256 = "4d6e40e3ab155f56b30de517380566a206d74b63cdf5ad49aa596924768f97e1"


def main() -> None:
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    archive = runtime / f"OBS-Studio-{VERSION}-Windows-x64.zip"
    destination = runtime / "obs"
    if not archive.exists():
        partial = archive.with_suffix(".part")
        request = urllib.request.Request(URL, headers={"User-Agent": "Noesis/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length", "0"))
            downloaded = 0
            next_report = 0
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                downloaded += len(chunk)
                if downloaded >= next_report:
                    print(f"OBS download: {downloaded // (1024 * 1024)} / {total // (1024 * 1024)} MiB", flush=True)
                    next_report += 32 * 1024 * 1024
        partial.replace(archive)
    digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    if digest != SHA256:
        raise RuntimeError("OBS archive checksum mismatch; refusing to extract")
    destination.mkdir(exist_ok=True)
    resolved = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(resolved):
                raise RuntimeError("Archive member escapes the portable OBS directory")
        bundle.extractall(destination)
    (destination / "portable_mode.txt").touch()
    print(f"Verified portable OBS: {destination / 'bin' / '64bit' / 'obs64.exe'}", flush=True)


if __name__ == "__main__":
    main()
