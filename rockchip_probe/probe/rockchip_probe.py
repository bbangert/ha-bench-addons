"""Proves, from inside an app container, that a Rockchip RK3566/RK3568's NPU,
GPU and video codecs work with the userspace bundled in this image.

Runs every selected probe once, then serves the report over HTTP until
stopped, so the app stays started and the result can be fetched headlessly.
"""

import datetime
import glob
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = "/opt/rockchip-probe"
DATA = "/data"
PORT = 8000
OUTPUT_LIMIT = 20000

ALL_PROBES = ["environment", "npu", "gpu", "vpu", "rga"]
SUPPORTED_SOCS = ("rockchip,rk3566", "rockchip,rk3568")

# Mirrors `devices:` in config.yaml, plus the Rockchip CMA heap some kernels add.
DEVICE_NODES = [
    "/dev/dri/card0", "/dev/dri/card1", "/dev/dri/renderD128", "/dev/dri/renderD129",
    "/dev/mali0", "/dev/mpp_service", "/dev/rga",
    "/dev/dma_heap/system", "/dev/dma_heap/system-uncached", "/dev/dma_heap/reserved",
    "/dev/rk_dma_heap/rk-dma-heap-cma",
]

# From the official example's README (rknn-toolkit-lite2/examples/resnet18).
EXPECTED_TOP5 = [812, 404, 657, 466, 895]
EXPECTED_NPU_DRIVER = "0.9.8"
NPU_LOAD_FILES = ["/proc/rknpu/load", "/sys/kernel/debug/rknpu/load"]
NPU_VERSION_FILES = ["/proc/rknpu/version", "/sys/kernel/debug/rknpu/version"]

MPP_CODING = {"h264": 7, "hevc": 0x01000004}

REPORT = {"status": "running"}
REPORT_LOCK = threading.Lock()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def log(msg):
    print(f"[rockchip_probe] {msg}", file=sys.stderr, flush=True)


def read_text(path, limit=65536):
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace").strip()
    except OSError:
        return None


def first_readable(paths):
    for p in paths:
        text = read_text(p)
        if text is not None:
            return p, text
    return None, None


def run(cmd, timeout, env=None, sample=None, sample_every=0.5):
    """Runs cmd with a hard deadline, optionally calling sample() while it runs.

    The child gets its own session so the whole group can be killed. A child
    stuck in an uninterruptible driver call cannot be reaped; it is abandoned
    and reported rather than waited on.
    """
    out, err = [], []
    result = {"cmd": cmd, "rc": None, "timed_out": False, "abandoned": False}
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            env={**os.environ, **(env or {})}, start_new_session=True,
        )
    except OSError as e:
        result["error"] = repr(e)
        return result

    def reader(stream, sink):
        for chunk in iter(lambda: stream.read(4096), b""):
            sink.append(chunk)

    threads = [threading.Thread(target=reader, args=(s, b), daemon=True)
               for s, b in ((proc.stdout, out), (proc.stderr, err))]
    for t in threads:
        t.start()

    while proc.poll() is None:
        remaining = timeout - (time.monotonic() - t0)
        if remaining <= 0:
            result["timed_out"] = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                result["abandoned"] = True
            break
        if sample:
            sample()
        time.sleep(min(sample_every, max(remaining, 0.01)))

    for t in threads:
        t.join(timeout=2)
    result["rc"] = proc.returncode
    result["seconds"] = round(time.monotonic() - t0, 2)
    result["stdout"] = b"".join(out).decode("utf-8", "replace")[-OUTPUT_LIMIT:]
    result["stderr"] = b"".join(err).decode("utf-8", "replace")[-OUTPUT_LIMIT:]
    return result


def output_of(r):
    return (r.get("stdout") or "") + (r.get("stderr") or "")


def failure_of(r):
    if r.get("error"):
        return r["error"]
    if r.get("timed_out"):
        return f"timed out after {r['seconds']} s" + (" (process could not be killed)" if r["abandoned"] else "")
    return f"exit {r['rc']}"


def keyvals(text):
    kv = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and re.fullmatch(r"[a-z_]+", k):
            kv[k] = v
    return kv


def read_device_tree(prop):
    # /proc/device-tree is what MPP and the RKNN examples read; /device-tree is
    # Vagus's read-only bind of the same tree.
    for base in ("/proc/device-tree", "/device-tree"):
        try:
            with open(os.path.join(base, prop), "rb") as f:
                return base, f.read()
        except OSError:
            continue
    return None, None


def soc_info():
    source, compat = read_device_tree("compatible")
    _, model = read_device_tree("model")
    compatible = [c for c in (compat or b"").decode("utf-8", "replace").split("\0") if c]
    return {
        "source": source,
        "compatible": compatible,
        "model": model.decode("utf-8", "replace").rstrip("\0") if model else None,
        "supported": any(c in SUPPORTED_SOCS for c in compatible),
    }


def describe_node(path):
    try:
        st = os.stat(path)
    except OSError as e:
        return {"path": path, "present": False, "error": e.strerror}
    kind = ("char" if stat.S_ISCHR(st.st_mode) else "block" if stat.S_ISBLK(st.st_mode)
            else "dir" if stat.S_ISDIR(st.st_mode) else "other")
    node = {"path": path, "present": True, "type": kind, "mode": oct(stat.S_IMODE(st.st_mode)),
            "uid": st.st_uid, "gid": st.st_gid}
    if kind in ("char", "block"):
        node["major_minor"] = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
        # Opening is the only way to see whether the device cgroup admits us.
        try:
            os.close(os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC))
            node["open_rw"] = "ok"
        except OSError as e:
            node["open_rw"] = e.strerror
    return node


def drm_sysfs():
    nodes = {}
    for d in sorted(glob.glob("/sys/class/drm/*")):
        name = os.path.basename(d)
        if not re.fullmatch(r"(card|renderD)\d+", name):
            continue
        uevent = read_text(os.path.join(d, "device", "uevent")) or ""
        m = re.search(r"^DRIVER=(.*)$", uevent, re.M)
        nodes[name] = m.group(1) if m else None
    return nodes


def components():
    items = []
    for line in (read_text(os.path.join(ROOT, "SOURCES")) or "").splitlines():
        parts = line.split()
        if len(parts) == 5 and not line.startswith("#"):
            name, version, sha, file, url = parts
            items.append({"name": name, "version": version, "sha256": sha, "file": file, "url": url})
    return items


def app_version():
    m = re.search(r'^version:\s*"?([^"\s]+)', read_text(os.path.join(ROOT, "config.yaml")) or "", re.M)
    return m.group(1) if m else None


def mali_kernel_info():
    info = {}
    for p in sorted(glob.glob("/sys/module/*kbase*/version")) + [
        "/sys/class/misc/mali0/device/gpuinfo", "/sys/kernel/debug/mali0/version",
    ]:
        text = read_text(p)
        if text is not None:
            info[p] = text
    return info


def probe_environment(ctx):
    soc = ctx["soc"]
    nodes = sorted(set(DEVICE_NODES) | set(glob.glob("/dev/dri/*")) | set(glob.glob("/dev/dma_heap/*"))
                   | set(glob.glob("/dev/rk_dma_heap/*")))
    raw = {
        "device_tree": soc,
        "kernel": {"release": os.uname().release, "version": os.uname().version,
                   "machine": os.uname().machine, "proc_version": read_text("/proc/version")},
        "devices": [describe_node(p) for p in nodes],
        "drm_sysfs_drivers": drm_sysfs(),
        "proc": {p: os.path.exists(p) for p in ("/proc/rknpu", "/proc/mpp_service", "/proc/rkrga")},
        "debugfs_mounted": os.path.isdir("/sys/kernel/debug/rknpu") or os.path.isdir("/sys/kernel/debug/rkrga"),
        "uid": os.getuid(),
        "image": {"app_version": app_version(), "components": components(),
                  "clips": json.loads(read_text(os.path.join(ROOT, "clips", "clips.json")) or "{}")},
    }
    present = [n["path"] for n in raw["devices"] if n.get("present")]
    missing = [p for p in DEVICE_NODES if p not in present]
    if not soc["compatible"] or not soc["model"]:
        return "fail", "device tree not readable at /proc/device-tree or /device-tree (is devicetree: true set?)", raw
    detail = f"{soc['model']}; {', '.join(soc['compatible'])}; kernel {os.uname().release}; " \
             f"{len(present)} device nodes present"
    if missing:
        detail += f"; absent: {', '.join(missing)}"
    return "pass", detail, raw


def parse_load(text):
    return [int(v) for v in re.findall(r"(\d+)\s*%", text or "")]


def probe_npu(ctx):
    npu = os.path.join(ROOT, "npu")
    loop_seconds = ctx["options"]["npu_loop_seconds"]
    load_file = next((p for p in NPU_LOAD_FILES if read_text(p) is not None), None)
    samples = []
    t0 = time.monotonic()

    def sample():
        if load_file:
            values = parse_load(read_text(load_file))
            samples.append({"t": round(time.monotonic() - t0, 2), "load_percent": values})

    # rknnlite refuses to start inside a container that has /.dockerenv unless
    # NPU_DRIVER_NODE (default /dev/dri/renderD129) exists, so point it at the
    # node sysfs says is the NPU.
    npu_nodes = [f"/dev/dri/{n}" for n, drv in drm_sysfs().items() if (drv or "").lower() == "rknpu"]
    env = {"NPU_DRIVER_NODE": next((n for n in npu_nodes if "renderD" in n), npu_nodes[0])} if npu_nodes else None

    r = run([sys.executable, os.path.join(ROOT, "npu_worker.py"),
             "--model", os.path.join(npu, "resnet18_for_rk3566_rk3568.rknn"),
             "--image", os.path.join(npu, "space_shuttle_224.jpg"),
             "--labels", os.path.join(npu, "synset_label.py"),
             "--loop-seconds", str(loop_seconds)],
            timeout=loop_seconds + 60, env=env, sample=sample)
    text = output_of(r)
    results = [json.loads(line[len("RESULT_JSON:"):]) for line in r.get("stdout", "").splitlines()
               if line.startswith("RESULT_JSON:")]
    result = results[-1] if results else {}

    runtime = re.search(r"librknnrt version:\s*(\S+)", text)
    driver = (re.search(r"RKNN Driver Information, version:\s*(\S+)", text)
              or re.search(r"driver version:\s*(\d+\.\d+\.\d+)", text, re.I)
              or re.search(r"DRV[^\n]*?(\d+\.\d+\.\d+)", str(result.get("sdk_version", ""))))
    version_file, version_text = first_readable(NPU_VERSION_FILES)
    max_load = max((v for s in samples for v in s["load_percent"]), default=None)
    raw = {
        "npu_drm_nodes": npu_nodes,
        "runtime_version": runtime.group(1) if runtime else None,
        "driver_version": driver.group(1) if driver else None,
        "kernel_version_file": {"path": version_file, "text": version_text},
        "result": result,
        "expected_top5": EXPECTED_TOP5,
        "load": {"file": load_file, "max_percent": max_load, "samples": samples[-240:]},
        "runtime_log": [ln for ln in text.splitlines() if "RKNN" in ln or "rknn" in ln][:50],
        "worker": r,
    }

    top = result.get("top5") or []
    indices = [t["index"] for t in top]
    problems = []
    if r["rc"] != 0:
        problems.append(f"worker {failure_of(r)}" + (f": {result['error']}" if result.get("error") else ""))
    if not top:
        problems.append("no inference result")
    elif indices[0] != EXPECTED_TOP5[0] or set(indices) != set(EXPECTED_TOP5):
        problems.append(f"TOP5 {indices} != expected {EXPECTED_TOP5}")

    parts = []
    if top:
        parts.append(f"TOP1 [{top[0]['index']}] {top[0]['label']} {top[0]['score']}")
        parts.append("TOP5 order " + ("matches" if indices == EXPECTED_TOP5 else "differs from") + " the example")
    if raw["runtime_version"]:
        parts.append(f"librknnrt {raw['runtime_version']}")
    if raw["driver_version"]:
        note = "" if raw["driver_version"] == EXPECTED_NPU_DRIVER else f" (expected {EXPECTED_NPU_DRIVER}; reported only)"
        parts.append(f"driver {raw['driver_version']}{note}")
    else:
        parts.append("driver version not found in runtime output")
    if loop := result.get("loop"):
        parts.append(f"loop {loop['inferences']} inferences in {loop['seconds']} s "
                     f"({loop['per_second']}/s, avg {loop['ms_avg']} ms)")
    if load_file:
        parts.append(f"NPU load max {max_load}% from {load_file}" if max_load is not None
                     else f"NPU load unreadable at {load_file}")
    else:
        parts.append("NPU load not available in the container")
    if problems:
        return "fail", "; ".join(problems + parts), raw
    return "pass", "; ".join(parts), raw


def probe_gpu(ctx):
    egl = "/usr/local/bin/eglprobe"
    cards = sorted(glob.glob("/dev/dri/card*"))
    renders = sorted(glob.glob("/dev/dri/renderD*"))
    names = {}
    for node in cards + renders:
        r = run([egl, "drm", node], timeout=10)
        names[node] = keyvals(r.get("stdout", "")).get("drm_driver") or failure_of(r)
    raw = {"drm_drivers": names, "mali_kernel": mali_kernel_info(), "runs": {}}

    for mode in ("surfaceless", "default"):
        r = run([egl, mode], timeout=30)
        raw["runs"][mode] = {"expected_to_fail": True, "ok": r["rc"] == 0, **keyvals(r.get("stdout", "")),
                             "process": r}

    card = next((n for n in cards if names.get(n) == "rockchip"), None)
    if not card:
        return "fail", f"no rockchip-drm card node among {cards or 'none'} ({names})", raw
    r = run([egl, "gbm", card], timeout=30)
    kv = keyvals(r.get("stdout", ""))
    raw["runs"]["gbm"] = {"node": card, "ok": r["rc"] == 0, **kv, "process": r}

    problems = []
    if r["rc"] != 0:
        problems.append(f"eglprobe gbm {failure_of(r)}" + (f" ({kv['error']})" if kv.get("error") else ""))
    if not kv.get("gl_version", "").startswith("OpenGL ES 3."):
        problems.append(f"GL_VERSION {kv.get('gl_version')!r}")
    if "Mali-G52" not in kv.get("gl_renderer", ""):
        problems.append(f"GL_RENDERER {kv.get('gl_renderer')!r}")
    if kv.get("fbo_status") != "0x8cd5":
        problems.append(f"FBO status {kv.get('fbo_status')}")
    if kv.get("pixel") != "255,0,0,255":
        problems.append(f"pixel {kv.get('pixel')}")

    others = ", ".join(f"{m} {'ok' if raw['runs'][m]['ok'] else 'failed'}" for m in ("surfaceless", "default"))
    kernel = "; ".join(f"{k}: {v}" for k, v in raw["mali_kernel"].items()) or "kernel Mali version not readable"
    detail = (f"gbm on {card}: {kv.get('gl_version')} | {kv.get('gl_renderer')} | fbo {kv.get('fbo_status')} | "
              f"pixel {kv.get('pixel')}; {others} (informational); {kernel}")
    return ("fail", "; ".join(problems) + "; " + detail, raw) if problems else ("pass", detail, raw)


def mpp_decode(path, codec, expected_frames):
    # mpp_hal_debug=2 makes MPP log which hardware HAL it bound ("found api
    # h264d_vdpu34x for coding 7 client N"); MPP has no software decoder, so a
    # bound HAL is the hardware path. mpp_debug=0x10 logs the SoC it matched.
    r = run(["/usr/local/bin/mpi_dec_test", "-t", str(MPP_CODING[codec]), "-i", path], timeout=60,
            env={"mpp_hal_debug": "2", "mpp_debug": "16"})
    text = output_of(r)
    api = re.findall(r"found api (\S+) for coding \d+ client (\d+)", text)
    frames = re.search(r"decode (\d+) frames time (\d+) ms .*?fps\s*([\d.]+)", text)
    chip = re.search(r"match chip name: (\S+)", text)
    errors = len(re.findall(r"decode get frame \d+.* err [0-9a-f]+ discard", text))
    got = int(frames.group(1)) if frames else None
    check = {
        "file": os.path.basename(path), "codec": codec, "expected_frames": expected_frames,
        "frames": got, "fps": float(frames.group(3)) if frames else None,
        "hal": [a[0] for a in api], "client_type": [int(a[1]) for a in api],
        "soc_matched": chip.group(1) if chip else None, "error_frames": errors, "process": r,
    }
    problems = []
    if r["rc"] != 0:
        problems.append(failure_of(r))
    if not api:
        problems.append("no hardware HAL bound")
    if got is None:
        problems.append("no frame count in output")
    elif got != expected_frames:
        problems.append(f"{got} frames, expected {expected_frames}")
    if errors:
        problems.append(f"{errors} frames with errors")
    check["ok"] = not problems
    summary = (f"{codec} {'?' if got is None else got}/{expected_frames} frames via {','.join(check['hal']) or '?'}"
               + (f" at {check['fps']} fps" if check["fps"] else ""))
    return check, ("; ".join([summary] + problems) if problems else summary)


def probe_vpu(ctx):
    clips_dir = os.path.join(ROOT, "clips")
    clips = json.loads(read_text(os.path.join(clips_dir, "clips.json")) or "{}")
    info = run(["/usr/local/bin/mpp_info_test"], timeout=20)
    raw = {
        "mpp_info_test": info,
        "proc_mpp_service": {p: read_text(p) for p in (
            "/proc/mpp_service/version", "/proc/mpp_service/supports-device", "/proc/mpp_service/supports-cmd")},
        "decode": [],
    }
    parts, ok = [], True

    for name, meta in clips.items():
        check, summary = mpp_decode(os.path.join(clips_dir, name), meta["codec"], int(meta["frames"]))
        raw["decode"].append(check)
        parts.append(summary)
        ok &= check["ok"]

    out = "/tmp/rockchip-probe-enc.h264"
    if os.path.exists(out):
        os.unlink(out)
    r = run(["/usr/local/bin/mpi_enc_test", "-t", "7", "-w", "1280", "-h", "720", "-n", "30", "-o", out],
            timeout=60, env={"mpp_enc_hal_debug": "2", "mpp_debug": "16"})
    text = output_of(r)
    size = os.path.getsize(out) if os.path.exists(out) else 0
    api = re.findall(r"found api (\S+) for coding \d+ client (\d+)", text)
    frames = re.search(r"encode (\d+) frames", text)
    enc_ok = r["rc"] == 0 and size > 0
    raw["encode"] = {"codec": "h264", "bytes": size, "frames": int(frames.group(1)) if frames else None,
                     "hal": [a[0] for a in api], "ok": enc_ok, "process": r}
    enc_summary = f"h264 encode {size} bytes via {','.join(raw['encode']['hal']) or '?'}"
    parts.append(enc_summary if enc_ok else f"{enc_summary}; {failure_of(r)}")
    ok &= enc_ok

    if not clips:
        ok = False
        parts.append("no test clips in the image")
    mpp_version = re.search(r"mpp version: (.*)", output_of(info))
    parts.append(f"mpp_info_test {'ok' if info['rc'] == 0 else failure_of(info)}"
                 + (f" (mpp version: {mpp_version.group(1).strip()[:80]})" if mpp_version else ""))
    return ("pass" if ok else "fail"), "; ".join(parts), raw


def probe_rga(ctx):
    raw = {"node": describe_node("/dev/rga"),
           "files": {p: read_text(p) for p in (
               "/sys/kernel/debug/rkrga/driver_version", "/sys/kernel/debug/rkrga/hardware",
               "/proc/rkrga/driver_version", "/proc/rkrga/hardware")}}
    node = raw["node"]
    version = next((v for p, v in raw["files"].items() if p.endswith("driver_version") and v), None)
    detail = (f"/dev/rga {node['major_minor']} open {node.get('open_rw')}" if node.get("present")
              else "/dev/rga absent")
    detail += f"; driver {version}" if version else "; driver version not visible in the container"
    return "reported", detail, raw


PROBES = {"environment": probe_environment, "npu": probe_npu, "gpu": probe_gpu, "vpu": probe_vpu,
          "rga": probe_rga}


def run_probe(name, ctx):
    started = now()
    t0 = time.monotonic()
    log(f"{name}: start")
    try:
        status, detail, raw = PROBES[name](ctx)
    except Exception as e:  # a probe bug must not take the report with it
        status, detail, raw = "fail", f"probe crashed: {e!r}", {}
    log(f"{name}: {status} — {detail}")
    return {"name": name, "status": status, "detail": detail, "started_at": started, "finished_at": now(),
            "seconds": round(time.monotonic() - t0, 2), "raw": raw}


def load_options():
    opts = {"probes": list(ALL_PROBES), "npu_loop_seconds": 10}
    notes = []
    try:
        with open(os.path.join(DATA, "options.json")) as f:
            given = json.load(f)
    except (OSError, ValueError) as e:
        given = {}
        notes.append(f"options.json not used: {e}")
    if isinstance(given.get("probes"), list):
        unknown = [p for p in given["probes"] if p not in ALL_PROBES]
        if unknown:
            notes.append(f"ignored unknown probes {unknown}")
        opts["probes"] = [p for p in ALL_PROBES if p in given["probes"]]
    if isinstance(given.get("npu_loop_seconds"), (int, float)):
        opts["npu_loop_seconds"] = max(0, min(120, int(given["npu_loop_seconds"])))
    return opts, notes


def write_report(report):
    body = json.dumps(report)
    for directory in (DATA, "/tmp"):
        try:
            tmp = os.path.join(directory, "report.json.tmp")
            with open(tmp, "w") as f:
                f.write(body)
            os.replace(tmp, os.path.join(directory, "report.json"))
            return os.path.join(directory, "report.json")
        except OSError:
            continue
    return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/report.json"):
            with REPORT_LOCK:
                body = json.dumps(REPORT, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            body = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log("http " + fmt % args)


def main():
    # PID 1 ignores signals that have no handler, so `stop` would wait for the kill timeout.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))

    server = ThreadingHTTPServer(("", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"serving the report on :{PORT}")

    options, notes = load_options()
    soc = soc_info()
    forced = os.environ.get("ROCKCHIP_PROBE_IGNORE_SOC") == "1"
    started = now()
    t0 = time.monotonic()
    report = {"app": "rockchip_probe", "version": app_version(), "status": "running", "started_at": started,
              "options": options, "option_notes": notes, "soc": {**soc, "check_ignored": forced}, "probes": []}
    with REPORT_LOCK:
        REPORT.clear()
        REPORT.update(report)

    ctx = {"options": options, "soc": soc}
    unsupported = not soc["supported"] and not forced
    reason = "unsupported SoC: " + (", ".join(soc["compatible"]) or "device tree not readable")
    for name in options["probes"]:
        if unsupported and name != "environment":
            result = {"name": name, "status": "skipped", "detail": reason, "started_at": now(),
                      "finished_at": now(), "seconds": 0, "raw": {}}
        else:
            result = run_probe(name, ctx)
        with REPORT_LOCK:
            REPORT["probes"].append(result)

    counts = {s: sum(1 for p in REPORT["probes"] if p["status"] == s)
              for s in ("pass", "fail", "skipped", "reported")}
    overall = "unsupported SoC" if unsupported else ("fail" if counts["fail"] else "pass")
    with REPORT_LOCK:
        REPORT.update({"status": "done", "finished_at": now(), "seconds": round(time.monotonic() - t0, 2),
                       "summary": {"overall": overall, **counts, **({"reason": reason} if unsupported else {})}})
        final = json.loads(json.dumps(REPORT))
    path = write_report(final)
    log(f"done: {overall} {counts}; report at {path} and http://<host>:<mapped port>/report.json")
    print(json.dumps(final), flush=True)

    while True:
        signal.pause()


if __name__ == "__main__":
    main()
