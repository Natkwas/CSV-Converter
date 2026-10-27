"""Self-update check/download for the packaged (frozen) desktop app.

Hosted for free on GitHub Releases: the repo is public, so the GitHub API
(latest-release lookup) and the release's zip asset both download with no
auth needed. Does nothing when run from source (only the PyInstaller build
has a folder structure that's actually safe to replace).

To ship a new version: bump APP_VERSION below, rebuild the .exe, zip the
dist/BankStatementConverter folder (the zip must contain that single
top-level folder, matching the existing dist/BankStatementConverter.zip
layout), and publish it as a GitHub Release tagged "vX.Y.Z" with the zip
attached as an asset.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

import requests

APP_VERSION = "1.0.0"

# Set once the GitHub repo exists, e.g. "yourusername/bank-statement-converter".
GITHUB_REPO = os.environ.get("UPDATE_REPO", "Natkwas/CSV-Converter")

TIMEOUT = 15


class UpdateError(Exception):
    pass


def _parse_version(tag: str) -> tuple[int, ...]:
    tag = tag.lstrip("vV")
    parts = []
    for p in tag.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_frozen() -> bool:
    """True only for the packaged .exe -- updating a source checkout isn't
    this feature's job (and there's no single folder to safely swap)."""
    return bool(getattr(sys, "frozen", False))


def check_for_update() -> dict | None:
    """Returns {"version", "notes", "download_url"} if a newer release is
    published on GitHub, else None. Raises UpdateError on a network/API
    failure -- callers should treat that as "couldn't check", not an error
    worth bothering the user with.
    """
    if not GITHUB_REPO:
        return None
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise UpdateError(f"Couldn't check for updates: {exc}") from exc
    if resp.status_code == 404:
        return None  # repo has no releases yet
    if resp.status_code >= 400:
        raise UpdateError(f"Couldn't check for updates (HTTP {resp.status_code})")

    data = resp.json()
    tag = data.get("tag_name") or ""
    if not tag or _parse_version(tag) <= _parse_version(APP_VERSION):
        return None

    asset = next((a for a in data.get("assets", []) if a["name"].lower().endswith(".zip")), None)
    if asset is None:
        return None

    return {
        "version": tag,
        "notes": data.get("body") or "",
        "download_url": asset["browser_download_url"],
    }


def download_update(download_url: str, on_progress=None) -> str:
    """Downloads the release zip to a temp file and returns its path.
    on_progress(fraction: float | None) is called periodically (None when
    the server doesn't report a content length, i.e. progress is unknown).
    """
    try:
        resp = requests.get(download_url, stream=True, timeout=60)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise UpdateError(f"Download failed: {exc}") from exc

    total = int(resp.headers.get("content-length") or 0)
    done = 0
    fd, path = tempfile.mkstemp(suffix=".zip", prefix="bsc_update_")
    with os.fdopen(fd, "wb") as f:
        for chunk in resp.iter_content(chunk_size=262144):
            if not chunk:
                continue
            f.write(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done / total if total else None)
    return path


def _extracted_app_root(extract_dir: str) -> str:
    """The release zip mirrors dist/BankStatementConverter.zip's layout --
    one top-level folder containing the exe -- so unwrap that one level if
    present; otherwise the extracted contents themselves are the app root.
    """
    entries = os.listdir(extract_dir)
    if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
        return os.path.join(extract_dir, entries[0])
    return extract_dir


def apply_update_and_restart(zip_path: str) -> None:
    """Extracts the downloaded zip, then hands off to a small helper batch
    script that waits for this process to exit, swaps the app folder, and
    relaunches -- a running .exe's own files can't be replaced from inside
    itself on Windows. The caller must actually terminate the process
    (os._exit) right after this returns, so the batch script's wait loop
    finishes quickly -- this function only sets the handoff up.
    """
    if not is_frozen():
        raise UpdateError("Auto-update only applies to the packaged app, not a source run.")

    app_dir = os.path.dirname(sys.executable)
    exe_name = os.path.basename(sys.executable)

    extract_dir = tempfile.mkdtemp(prefix="bsc_update_extract_")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"Downloaded update was corrupt: {exc}") from exc
    new_app_dir = _extracted_app_root(extract_dir)

    script_path = os.path.join(tempfile.gettempdir(), "bsc_apply_update.bat")
    script = f"""@echo off
setlocal
set PID={os.getpid()}
set APPDIR={app_dir}
set NEWDIR={new_app_dir}
set EXE={exe_name}
:wait
tasklist /fi "PID eq %PID%" | find "%PID%" >nul
if %errorlevel%==0 (
    timeout /t 1 /nobreak >nul
    goto wait
)
rmdir /s /q "%APPDIR%"
move "%NEWDIR%" "%APPDIR%"
start "" "%APPDIR%\\%EXE%"
(goto) 2>nul & del "%~f0"
"""
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    subprocess.Popen(
        ["cmd", "/c", script_path],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
