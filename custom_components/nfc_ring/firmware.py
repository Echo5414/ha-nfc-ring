"""Firmware images offered to the rings: the local folder and GitHub releases.

Images are MCUboot-signed .bin files. The newest one wins, whether it was
dropped into <config>/nfc_ring_firmware by hand or published as the asset of
the latest release in FIRMWARE_REPOSITORY. A release image is downloaded
into the releases/ subfolder right before it is installed. That cache only
counts while it matches the current release, so a withdrawn release is not
offered again, and it never mixes with hand-placed images.

Nothing here has to be trusted: the ring's bootloader refuses any image that
is not signed with the project key. The checks below only avoid sending a
ring something that is obviously broken.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import os
from pathlib import Path
import tempfile

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import coap

_LOGGER = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
MAX_IMAGE_SIZE = 1024 * 1024

_API_TIMEOUT = aiohttp.ClientTimeout(total=30)
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300)
_DOWNLOAD_CHUNK = 64 * 1024


class FirmwareError(Exception):
    """A firmware image could not be found, fetched or validated."""


@dataclass(frozen=True)
class Firmware:
    version: str
    size: int
    path: Path | None = None  # local image; None for a release not downloaded yet
    url: str | None = None  # release asset to download
    release_url: str | None = None
    release_notes: str | None = None


def newest(*candidates: Firmware | None) -> Firmware | None:
    """Highest version; on a tie the earlier candidate (pass local first)."""
    best = None
    for firmware in candidates:
        if firmware is not None and (
            best is None or coap.version_key(firmware.version) > coap.version_key(best.version)
        ):
            best = firmware
    return best


def find_local(directory: Path) -> Firmware | None:
    """Newest valid image in directory (blocking, run it in the executor)."""
    best: Firmware | None = None
    try:
        files = sorted(directory.glob("*.bin"))
    except OSError:
        return None

    for path in files:
        try:
            size = path.stat().st_size
            if size > MAX_IMAGE_SIZE:
                raise ValueError(f"larger than {MAX_IMAGE_SIZE} bytes")
            with path.open("rb") as file:
                version = coap.image_version(file.read(32))
        except (OSError, ValueError) as err:
            _LOGGER.warning("Ignoring firmware file %s: %s", path.name, err)
            continue
        best = newest(best, Firmware(version, size, path=path))
    return best


async def async_fetch_latest_release(hass: HomeAssistant, repository: str) -> Firmware | None:
    """The image attached to the latest release, or None if there is none."""
    session = async_get_clientsession(hass)
    url = f"{GITHUB_API}/repos/{repository}/releases/latest"
    try:
        async with session.get(
            url, headers={"Accept": "application/vnd.github+json"}, timeout=_API_TIMEOUT
        ) as resp:
            if resp.status == 404:
                _LOGGER.debug("No firmware release published in %s", repository)
                return None
            resp.raise_for_status()
            release = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise FirmwareError(f"could not check {repository} for firmware: {err}") from err

    if not isinstance(release, dict):
        raise FirmwareError(f"unexpected answer from {url}")

    tag = release.get("tag_name")
    version = tag.removeprefix("v") if isinstance(tag, str) else ""
    if not coap.is_version(version):
        raise FirmwareError(f"latest release tag {tag!r} in {repository} is not a version")

    assets = release.get("assets")
    asset = next(
        (
            a
            for a in (assets if isinstance(assets, list) else [])
            if isinstance(a, dict) and str(a.get("name", "")).endswith(".bin")
        ),
        None,
    )
    if asset is None:
        raise FirmwareError(f"release {tag} in {repository} has no .bin image")

    size = asset.get("size")
    download_url = asset.get("browser_download_url")
    if not (isinstance(size, int) and 0 < size <= MAX_IMAGE_SIZE):
        raise FirmwareError(f"release {tag}: image size {size!r} is not plausible")
    if not (isinstance(download_url, str) and download_url.startswith("https://")):
        raise FirmwareError(f"release {tag}: no usable download URL")

    notes = release.get("body")
    html_url = release.get("html_url")
    return Firmware(
        version,
        size,
        url=download_url,
        release_url=html_url if isinstance(html_url, str) else None,
        release_notes=notes if isinstance(notes, str) and notes.strip() else None,
    )


def _cache_path(cache_dir: Path, firmware: Firmware) -> Path:
    return cache_dir / f"nfc-ring-{firmware.version}.bin"


def cached_release(cache_dir: Path, firmware: Firmware) -> Firmware:
    """The release with path set if it is already downloaded (blocking)."""
    path = _cache_path(cache_dir, firmware)
    try:
        if path.stat().st_size == firmware.size:
            with path.open("rb") as file:
                if coap.image_version(file.read(32)) == firmware.version:
                    return replace(firmware, path=path)
    except (OSError, ValueError):
        pass
    return replace(firmware, path=None)


async def async_download(hass: HomeAssistant, firmware: Firmware, cache_dir: Path) -> Firmware:
    """Downloads a release image into cache_dir and returns it with its path."""
    if firmware.url is None:
        raise FirmwareError(f"firmware {firmware.version} has no download URL")

    session = async_get_clientsession(hass)
    data = bytearray()
    try:
        async with session.get(firmware.url, timeout=_DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK):
                data += chunk
                if len(data) > MAX_IMAGE_SIZE:
                    raise FirmwareError(f"image is larger than {MAX_IMAGE_SIZE} bytes")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise FirmwareError(f"downloading firmware {firmware.version} failed: {err}") from err

    if len(data) != firmware.size:
        raise FirmwareError(f"downloaded {len(data)} bytes, the release lists {firmware.size}")
    try:
        version = coap.image_version(bytes(data))
    except ValueError as err:
        raise FirmwareError(f"release image: {err}") from err
    if version != firmware.version:
        raise FirmwareError(f"image is version {version}, the release says {firmware.version}")

    path = _cache_path(cache_dir, firmware)
    try:
        await hass.async_add_executor_job(_store_release, path, bytes(data))
    except OSError as err:
        raise FirmwareError(f"could not save firmware {firmware.version}: {err}") from err
    _LOGGER.info("Downloaded NFC Ring firmware %s", firmware.version)
    return replace(firmware, path=path)


def _store_release(path: Path, data: bytes) -> None:
    """Writes path atomically and drops older cached releases (blocking)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".download-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    for old in path.parent.glob("*.bin"):
        if old != path:
            old.unlink(missing_ok=True)
