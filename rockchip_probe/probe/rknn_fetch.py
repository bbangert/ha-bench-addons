"""Fetches the Rockchip RKNN files at first start instead of shipping them.

The image may not redistribute them, so they come from the pinned upstream
URLs in RUNTIME_SOURCES, and nothing is used before its sha256 matches.
"""

import hashlib
import os
import shutil
import time
import urllib.request
import zipfile

LINK_DIR = "/var/lib/rockchip-probe/rknn"  # /usr/lib/librknnrt.so points into this
PERSISTENT_DIR = "/data/rknn"
FALLBACK_DIR = "/tmp/rockchip-probe-rknn"
WHEEL_NAME = "rknn-toolkit-lite2"


def read_sources(path):
    entries = []
    for line in open(path, encoding="utf-8").read().splitlines():
        parts = line.split()
        if len(parts) == 5 and not line.startswith("#"):
            name, version, sha, file, url = parts
            entries.append({"name": name, "version": version, "sha256": sha, "file": file, "url": url})
    return entries


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def choose_dir():
    for directory, persistent in ((PERSISTENT_DIR, True), (FALLBACK_DIR, False)):
        try:
            os.makedirs(directory, exist_ok=True)
            probe = os.path.join(directory, ".write-test")
            with open(probe, "w") as f:
                f.write("ok")
            os.unlink(probe)
            note = None if persistent else f"{PERSISTENT_DIR} not writable; files kept in {FALLBACK_DIR} for this run only"
            return directory, persistent, note
        except OSError as e:
            last = e
    raise OSError(f"no writable directory for the RKNN files: {last}")


def download(url, dest, sha, deadline, attempts=3):
    tmp = f"{dest}.part-{os.getpid()}"
    error = None
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            error = error or "fetch deadline reached"
            break
        try:
            h = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=min(30, remaining)) as resp, open(tmp, "wb") as out:
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("fetch deadline reached during download")
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    h.update(chunk)
                    out.write(chunk)
            if h.hexdigest() != sha:
                # Not retried: the same URL serving other bytes is not transient.
                os.unlink(tmp)
                return False, f"sha256 mismatch: got {h.hexdigest()}"
            os.replace(tmp, dest)
            return True, None
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
    return False, error


def safe_extract(zip_path, dest):
    """Extracts a zip, refusing any member that would land outside dest."""
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            name = member.filename
            target = os.path.realpath(os.path.join(root, name))
            if os.path.isabs(name) or not (target == root or target.startswith(root + os.sep)):
                raise ValueError(f"unsafe path in archive: {name!r}")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"symlink in archive: {name!r}")
        z.extractall(root)


# Keyed by the wheel's hash, so a different wheel never reuses an old tree.
def unpack_wheel(wheel, sha, directory):
    target = os.path.join(directory, f"site-{sha[:16]}")
    if os.path.isfile(os.path.join(target, ".complete")):
        return target, "cached"
    tmp = f"{target}.part-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    safe_extract(wheel, tmp)
    open(os.path.join(tmp, ".complete"), "w").close()
    shutil.rmtree(target, ignore_errors=True)
    os.rename(tmp, target)
    return target, "unpacked"


def point_link(directory):
    tmp = f"{LINK_DIR}.tmp-{os.getpid()}"
    os.makedirs(os.path.dirname(LINK_DIR), exist_ok=True)
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(directory, tmp)
    os.replace(tmp, LINK_DIR)


def ensure(sources_path, allow_download, budget_seconds=120):
    t0 = time.monotonic()
    deadline = t0 + budget_seconds
    report = {"allow_download": allow_download, "files": [], "ok": False}
    try:
        directory, persistent, note = choose_dir()
    except OSError as e:
        report.update(error=str(e), seconds=round(time.monotonic() - t0, 2))
        return report
    report.update(directory=directory, persistent=persistent, note=note)

    ok = True
    for src in read_sources(sources_path):
        path = os.path.join(directory, src["file"])
        entry = {"name": src["name"], "file": src["file"], "url": src["url"], "sha256": src["sha256"]}
        if os.path.exists(path):
            if sha256_of(path) == src["sha256"]:
                entry["status"] = "cached"
                report["files"].append(entry)
                continue
            os.unlink(path)
            entry["replaced"] = "cached copy failed its sha256 check and was deleted"
        if not allow_download:
            entry.update(status="failed", error="not in the cache and rknn_fetch is false")
        else:
            t = time.monotonic()
            done, error = download(src["url"], path, src["sha256"], deadline)
            entry.update(status="downloaded" if done else "failed", seconds=round(time.monotonic() - t, 2))
            if error:
                entry["error"] = error
        ok &= entry["status"] != "failed"
        report["files"].append(entry)

    if ok:
        try:
            wheel = next(s for s in report["files"] if s["name"] == WHEEL_NAME)
            report["site_dir"], report["wheel"] = unpack_wheel(
                os.path.join(directory, wheel["file"]), wheel["sha256"], directory)
            point_link(directory)
            report["link"] = f"{LINK_DIR} -> {directory}"
        except Exception as e:
            ok = False
            report["error"] = f"{type(e).__name__}: {e}"
    report["ok"] = ok
    report["seconds"] = round(time.monotonic() - t0, 2)
    return report
