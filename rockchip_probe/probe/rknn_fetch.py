"""Fetches the Rockchip RKNN files at first start instead of shipping them.

The image may not redistribute them, so they come from the pinned upstream
URLs in RUNTIME_SOURCES, and nothing is used before its sha256 matches.
"""

import hashlib
import json
import os
import shutil
import time
import sys
import urllib.request
import zipfile

LINK_DIR = "/var/lib/rockchip-probe/rknn"  # /usr/lib/librknnrt.so points into this
PERSISTENT_DIR = "/data/rknn"
FALLBACK_DIR = "/tmp/rockchip-probe-rknn"
# Where the runtime and the unpacked wheel go when the cache cannot map code.
EXEC_DIRS = ("/var/lib/rockchip-probe/rknn-exec", "/tmp/rockchip-probe-rknn-exec")
WHEEL_NAME = "rknn-toolkit-lite2"


def read_sources(path):
    entries = []
    for line in open(path, encoding="utf-8").read().splitlines():
        parts = line.split()
        if len(parts) == 6 and not line.startswith("#"):
            name, version, sha, size, file, url = parts
            entries.append({"name": name, "version": version, "sha256": sha, "size": int(size), "file": file,
                            "url": url})
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


def noexec(directory):
    return bool(os.statvfs(directory).f_flag & os.ST_NOEXEC)


def choose_exec_dir():
    for directory in EXEC_DIRS:
        try:
            os.makedirs(directory, exist_ok=True)
            if not noexec(directory):
                return directory
        except OSError:
            continue
    raise OSError(f"no exec-capable directory among {', '.join(EXEC_DIRS)}")


def copy_verified(src, dest, sha):
    tmp = f"{dest}.part-{os.getpid()}"
    shutil.copyfile(src, tmp)
    if sha256_of(tmp) != sha:
        os.unlink(tmp)
        raise ValueError(f"copy of {os.path.basename(src)} failed its sha256 check")
    os.replace(tmp, dest)


class Refused(Exception):
    """The server answered, but not with the pinned file; retrying cannot help."""


def set_read_timeout(resp, seconds):
    # urllib exposes no per-read timeout; the socket under the response does.
    try:
        resp.fp.raw._sock.settimeout(seconds)
    except AttributeError:
        pass


def download(url, dest, sha, size, deadline, attempts=3):
    tmp = f"{dest}.part-{os.getpid()}"
    error = None
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            error = error or "fetch deadline reached"
            break
        got = 0
        try:
            h = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=min(30, remaining)) as resp, open(tmp, "wb") as out:
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > size:
                    raise Refused(f"Content-Length {length} exceeds the pinned {size} bytes")
                while True:
                    # read1 returns after one socket read, so a server trickling
                    # bytes cannot hold a single call past the deadline.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"fetch deadline reached after {got} of {size} bytes")
                    set_read_timeout(resp, min(30, remaining))
                    chunk = resp.read1(1 << 16)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > size:
                        raise Refused(f"body exceeds the pinned {size} bytes")
                    h.update(chunk)
                    out.write(chunk)
            if h.hexdigest() != sha:
                raise Refused(f"sha256 mismatch: got {h.hexdigest()} ({got} bytes)")
            os.replace(tmp, dest)
            return True, None
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            if time.monotonic() >= deadline - 0.5:
                error = f"fetch deadline reached with {got} of {size} bytes received ({error})"
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if isinstance(e, Refused):
                break
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


# Only the wheel's hash is checked, so an extracted tree from an earlier start
# is never trusted: it is rebuilt from the verified wheel every time, and any
# other site-* tree is removed.
def unpack_wheel(wheel, sha, directory):
    target = os.path.join(directory, f"site-{sha[:16]}")
    tmp = f"{target}.part-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    safe_extract(wheel, tmp)
    old = f"{target}.old-{os.getpid()}"
    if os.path.lexists(target):
        os.rename(target, old)
    os.rename(tmp, target)
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if name.startswith("site-") and path != target:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
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
    # A fetch killed at its hard limit leaves partial downloads behind.
    for d in (directory,) + EXEC_DIRS:
        try:
            for name in os.listdir(d):
                if ".part-" in name:
                    path = os.path.join(d, name)
                    shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)
        except OSError:
            pass

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
            done, error = download(src["url"], path, src["sha256"], src["size"], deadline)
            entry.update(status="downloaded" if done else "failed", seconds=round(time.monotonic() - t, 2))
            if error:
                entry["error"] = error
        ok &= entry["status"] != "failed"
        report["files"].append(entry)

    if ok:
        try:
            wheel = next(s for s in report["files"] if s["name"] == WHEEL_NAME)
            runtime = next(s for s in report["files"] if s["file"] == "librknnrt.so")
            exec_dir = directory
            # A noexec mount (a tmpfs /data, say) stores the files fine but
            # cannot mmap them as code, so the loadable parts move elsewhere.
            if noexec(directory):
                exec_dir = choose_exec_dir()
                copy_verified(os.path.join(directory, runtime["file"]), os.path.join(exec_dir, runtime["file"]),
                              runtime["sha256"])
                why = f"{directory} is mounted noexec; runtime and unpacked wheel are loaded from {exec_dir}"
                report["note"] = f"{report['note']}; {why}" if report.get("note") else why
            wheel_path = os.path.join(directory, wheel["file"])
            if sha256_of(wheel_path) != wheel["sha256"]:
                raise ValueError("wheel changed after verification")
            report["exec_dir"] = exec_dir
            report["site_dir"], report["wheel"] = unpack_wheel(wheel_path, wheel["sha256"], exec_dir)
            point_link(exec_dir)
            report["link"] = f"{LINK_DIR} -> {exec_dir}"
        except Exception as e:
            ok = False
            report["error"] = f"{type(e).__name__}: {e}"
    report["ok"] = ok
    report["seconds"] = round(time.monotonic() - t0, 2)
    return report


# Run as a child of the probe, which kills it at a hard limit: name resolution
# and TLS setup block in ways no per-read timeout covers.
if __name__ == "__main__":
    result = ensure(sys.argv[1], sys.argv[2] == "1", float(sys.argv[3]))
    print("FETCH_JSON:" + json.dumps(result), flush=True)
