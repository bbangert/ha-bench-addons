"""Proves, from inside an app container, that a Rockchip RK3566/RK3568 or
RK3576's NPU, GPU and video codecs work with the userspace bundled in this image.

Runs every selected probe once, then serves the report over HTTP until
stopped, so the app stays started and the result can be fetched headlessly.
"""

import datetime
import glob
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time

import finite
import rknn_fetch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = "/opt/rockchip-probe"
DATA = "/data"
PORT = 8000
# Only what is stored in the report is bounded; probes parse the full output.
OUTPUT_LIMIT = 20000
LOG_LINES = 40
RGA_RESULTS = "/tmp/rgaprobe-results.txt"

ALL_PROBES = ["environment", "npu", "gpu", "vpu", "rga"]
# The accelerator checks. environment alone proves nothing about them, so a
# run without at least one of these passing cannot be "pass".
GATING_PROBES = ("npu", "gpu", "vpu", "rga")
RGA_CASES = ("copy", "resize-nv12", "resize-rgba", "cvtcolor-bt601-limited", "cvtcolor-bt601-full",
             "letterbox-bt601-limited", "letterbox-bt601-full", "crop-rotate-flip", "fill", "fill-rgb888")
# Only dma-heap system buffers gate. The vendor RGA2 driver rejects user memory
# that is not physically contiguous, so va/va-wrap are reported like the other
# heaps (see the README).
RGA_GATING_BUFFERS = ("system",)
DEFAULT_OPTIONS = {"probes": list(ALL_PROBES), "npu_loop_seconds": 10, "rknn_fetch": True}
RUNTIME_SOURCES = os.path.join(ROOT, "RUNTIME_SOURCES")
RKNN_FETCH_SECONDS = 120
ENCODE_FRAMES = 30

# The only per-SoC choices. Order matters: the first entry is also what
# ROCKCHIP_PROBE_IGNORE_SOC=1 falls back to on an unlisted SoC. The libmali
# blob, MPP and librga are the same for both: JeffyCN/mirrors gpu-chips.txt
# maps rk3566_rk3568 and rk3576 to bifrost-g52, and MPP/librga carry both.
SOCS = [
    {"name": "rk3566", "compatible": ("rockchip,rk3566", "rockchip,rk3568"),
     "model_entry": "resnet18-model-rk3566", "model_file": "resnet18_for_rk3566_rk3568.rknn",
     # MPP's SoC table: RK356[678] -> VEPU541 (osal/mpp_soc.c, mpp/soc.cmake).
     "encoder_hal": "hal_h264e_vepu541",
     # The example README's published result.
     "expected_top5": [812, 404, 657, 466, 895]},
    {"name": "rk3576", "compatible": ("rockchip,rk3576",),
     "model_entry": "resnet18-model-rk3576", "model_file": "resnet18_for_rk3576.rknn",
     "encoder_hal": "hal_h264e_vepu510",
     # Measured on a ROCK 4D: the RK3576 model ranks 833 above 466 and 895 drops
     # out, so the example's list would fail a working NPU.
     "expected_top5": [812, 404, 657, 833, 466]},
]

# Mirrors `devices:` in config.yaml, plus the Rockchip CMA heap some kernels add.
DEVICE_NODES = [
    "/dev/dri/card0", "/dev/dri/card1", "/dev/dri/renderD128", "/dev/dri/renderD129",
    "/dev/mali0", "/dev/mpp_service", "/dev/rga",
    "/dev/dma_heap/system", "/dev/dma_heap/system-uncached", "/dev/dma_heap/reserved",
    "/dev/rk_dma_heap/rk-dma-heap-cma",
]

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
    result["stdout"] = b"".join(out).decode("utf-8", "replace")
    result["stderr"] = b"".join(err).decode("utf-8", "replace")
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
    entry = next((e for e in SOCS if any(c in e["compatible"] for c in compatible)), None)
    return {
        "source": source,
        "compatible": compatible,
        "model": model.decode("utf-8", "replace").rstrip("\0") if model else None,
        "supported": entry is not None,
        "name": entry["name"] if entry else None,
    }


def soc_choices(soc):
    """The table entry to run with, and whether it was a guess for an unlisted SoC."""
    entry = next((e for e in SOCS if e["name"] == soc["name"]), None)
    return (entry, False) if entry else (SOCS[0], True)


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


MPP_DT_NOTE = ("only /device-tree/compatible is readable: MPP reads /proc/device-tree and will fall back to "
               "default codec capabilities")


def device_tree_access():
    access = {p: read_text(p) is not None for p in ("/proc/device-tree/compatible", "/device-tree/compatible")}
    notes = []
    if access["/device-tree/compatible"] and not access["/proc/device-tree/compatible"]:
        notes.append(MPP_DT_NOTE)
    return access, notes


def probe_environment(ctx):
    soc = ctx["soc"]
    dt_access, notes = device_tree_access()
    choice, guessed = soc_choices(soc)
    nodes = sorted(set(DEVICE_NODES) | set(glob.glob("/dev/dri/*")) | set(glob.glob("/dev/dma_heap/*"))
                   | set(glob.glob("/dev/rk_dma_heap/*")))
    raw = {
        "device_tree": {**soc, "readable": dt_access, "soc_check_used": soc["source"]},
        "per_soc": {**{k: v for k, v in choice.items() if k != "compatible"}, "guessed_for_unlisted_soc": guessed},
        "notes": notes,
        "kernel": {"release": os.uname().release, "version": os.uname().version,
                   "machine": os.uname().machine, "proc_version": read_text("/proc/version")},
        "devices": [describe_node(p) for p in nodes],
        "drm_sysfs_drivers": drm_sysfs(),
        "proc": {p: os.path.exists(p) for p in ("/proc/rknpu", "/proc/mpp_service", "/proc/rkrga")},
        "debugfs_mounted": os.path.isdir("/sys/kernel/debug/rknpu") or os.path.isdir("/sys/kernel/debug/rkrga"),
        "uid": os.getuid(),
        "rknn_files": ctx.get("rknn"),
        "image": {"app_version": app_version(), "components": components(),
                  "fetched_at_start": rknn_fetch.select_sources(rknn_fetch.read_sources(RUNTIME_SOURCES),
                                                                choice["model_entry"]),
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
    detail += f"; SoC check read {soc['source']}/compatible"
    detail += (f"; per-SoC choices from {choice['name']}"
               + (" (guessed: SoC not listed)" if guessed else "")
               + f": model {choice['model_file']}, encoder HAL {choice['encoder_hal']}")
    if notes:
        detail += "; note: " + "; ".join(notes)
    return "pass", detail, raw


def parse_load(text):
    return [int(v) for v in re.findall(r"(\d+)\s*%", text or "")]


def npu_loop_problems(result, loop_seconds):
    if loop_seconds <= 0:
        return []
    loop = result.get("loop")
    if not loop:
        return ["loop requested but did not complete"]
    problems = []
    if loop.get("inferences", 0) == 0:
        problems.append("loop completed zero inferences")
    if loop.get("top1_mismatches", 0) > 0:
        problems.append(f"loop TOP1 changed in {loop['top1_mismatches']} of {loop.get('inferences')} inferences")
    if loop.get("non_finite_inferences", 0) > 0:
        problems.append(f"loop produced non-finite outputs in {loop['non_finite_inferences']} inferences")
    return problems


def non_finite_problems(result, top):
    # NaN compares false with everything, so a NaN score could otherwise sit
    # behind a correct-looking class index.
    problems = []
    if result.get("non_finite_outputs"):
        problems.append(f"reference inference produced {result['non_finite_outputs']} non-finite output values")
    scores = [t.get("score") for t in top]
    if any(not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")) for v in scores):
        problems.append(f"TOP5 scores are not all finite: {scores}")
    if result.get("non_finite_values"):
        problems.append("non-finite numbers in the worker result: " + "; ".join(result["non_finite_values"][:5]))
    return problems


def rknn_unavailable(rknn):
    if not rknn:
        return "RKNN files were not fetched"
    reasons = [f"{f['file']}: {f.get('error', f['status'])}" for f in rknn.get("files", []) if f["status"] == "failed"]
    if rknn.get("error"):
        reasons.append(rknn["error"])
    return "; ".join(reasons) or "unknown"


def probe_npu(ctx):
    rknn = ctx.get("rknn") or {}
    if not rknn.get("ok"):
        return "fail", f"RKNN files not available: {rknn_unavailable(rknn)}", {"rknn_files": rknn}
    npu = rknn["directory"]
    expected_top5 = soc_choices(ctx["soc"])[0]["expected_top5"]
    worker = os.path.join(ROOT, "npu_worker.py")
    paths = {"model": os.path.join(npu, soc_choices(ctx["soc"])[0]["model_file"]),
             "image": os.path.join(npu, "space_shuttle_224.jpg"),
             "labels": os.path.join(npu, "synset_label.py")}
    rknn_env = {"PYTHONPATH": rknn["site_dir"],
                "LIBRKNNRT_PATH": os.path.join(rknn.get("exec_dir", npu), "librknnrt.so")}
    check = run([sys.executable, worker, "--selfcheck", "--image", paths["image"]], timeout=60, env=rknn_env)
    selfcheck = next((json.loads(line[len("SELFCHECK_JSON:"):]) for line in check.get("stdout", "").splitlines()
                      if line.startswith("SELFCHECK_JSON:")), {})
    if not selfcheck.get("ok"):
        return "fail", f"RKNN self-check failed after fetch: {selfcheck or failure_of(check)}", {
            "rknn_files": rknn, "selfcheck": selfcheck, "selfcheck_process": check}
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
    env = dict(rknn_env)
    if npu_nodes:
        env["NPU_DRIVER_NODE"] = next((n for n in npu_nodes if "renderD" in n), npu_nodes[0])

    r = run([sys.executable, worker, "--model", paths["model"], "--image", paths["image"],
             "--labels", paths["labels"], "--loop-seconds", str(loop_seconds)],
            timeout=loop_seconds + 60, env=env, sample=sample)
    text = output_of(r)
    results = []
    for line in r.get("stdout", "").splitlines():
        if line.startswith("RESULT_JSON:"):
            try:
                results.append(json.loads(line[len("RESULT_JSON:"):]))
            except ValueError as e:
                results.append({"error": f"unparseable worker result: {e}"})
    result = results[-1] if results else {}

    runtime = re.search(r"librknnrt version:\s*(\S+)", text)
    driver = (re.search(r"RKNN Driver Information, version:\s*(\S+)", text)
              or re.search(r"driver version:\s*(\d+\.\d+\.\d+)", text, re.I)
              or re.search(r"DRV[^\n]*?(\d+\.\d+\.\d+)", str(result.get("sdk_version", ""))))
    version_file, version_text = first_readable(NPU_VERSION_FILES)
    max_load = max((v for s in samples for v in s["load_percent"]), default=None)
    raw = {
        "npu_drm_nodes": npu_nodes,
        "rknn_files": {k: v for k, v in rknn.items() if k != "files"},
        "selfcheck": selfcheck,
        "runtime_version": runtime.group(1) if runtime else None,
        "driver_version": driver.group(1) if driver else None,
        "kernel_version_file": {"path": version_file, "text": version_text},
        "result": result,
        "expected_top5": expected_top5,
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
    elif indices[0] != expected_top5[0] or set(indices) != set(expected_top5):
        problems.append(f"TOP5 {indices} != expected {expected_top5}")
    if top:
        problems += non_finite_problems(result, top)
        problems += npu_loop_problems(result, loop_seconds)

    parts = []
    if top:
        parts.append(f"TOP1 [{top[0]['index']}] {top[0]['label']} {top[0]['score']}")
        parts.append("TOP5 order " + ("matches" if indices == expected_top5 else "differs from") + " the expected order")
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
        raw["runs"][mode] = {"gating": False, "ok": r["rc"] == 0, **keyvals(r.get("stdout", "")),
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
    soc_log = {
        "compatible_open_error": "open /proc/device-tree/compatible error" in text,
        "no_soc_match": (m.group(1).strip() if (m := re.search(r"can not found match soc name: ?(.*)", text)) else None),
        "default_chip_info": "use default chip info" in text,
    }
    errors = len(re.findall(r"decode get frame \d+.* err [0-9a-f]+ discard", text))
    got = int(frames.group(1)) if frames else None
    check = {
        "file": os.path.basename(path), "codec": codec, "expected_frames": expected_frames,
        "frames": got, "fps": float(frames.group(3)) if frames else None,
        "hal": [a[0] for a in api], "client_type": [int(a[1]) for a in api],
        "soc_matched": chip.group(1) if chip else None, "mpp_soc_log": soc_log, "error_frames": errors,
        "process": r,
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


def encode_check(r, size, requested, expected_hal):
    text = output_of(r)
    # The bound encoder HAL is named in the mpp_enc_hal_debug "found api" line,
    # or as the log tag ("mpp[pid]: hal_h264e_x: ...") of its own messages. The
    # debug flag also logs every HAL merely registered ("api hal_... adding"),
    # so a bare name anywhere in the text proves nothing.
    hal = re.findall(r"found api (\S+) for coding \d+ client \d+", text)
    hal = hal or sorted(set(re.findall(r"^\S+: (hal_h26[45]e_\w+): ", text, re.M)))
    frames = re.search(r"encode (\d+) frames", text)
    got = int(frames.group(1)) if frames else None
    problems = []
    if r["rc"] != 0:
        problems.append(failure_of(r))
    if size <= 0:
        problems.append("empty output stream")
    if got != requested:
        problems.append(f"{'no' if got is None else got} frames encoded, expected {requested}")
    if not hal:
        problems.append("no encoder HAL bound")
    # MPP has no software encoder, so any bound HAL is hardware; a different
    # one from the table is worth seeing but is not a failure.
    check = {"codec": "h264", "bytes": size, "requested_frames": requested, "frames": got, "hal": hal,
             "expected_hal": expected_hal, "hal_as_expected": bool(hal) and all(h == expected_hal for h in hal),
             "ok": not problems, "process": r}
    summary = f"h264 encode {'?' if got is None else got}/{requested} frames, {size} bytes via {','.join(hal) or '?'}"
    if hal and not check["hal_as_expected"]:
        summary += f" (expected {expected_hal}; reported only)"
    return check, "; ".join([summary] + problems)


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
    r = run(["/usr/local/bin/mpi_enc_test", "-t", "7", "-w", "1280", "-h", "720", "-n", str(ENCODE_FRAMES),
             "-o", out], timeout=60, env={"mpp_enc_hal_debug": "2", "mpp_debug": "16"})
    size = os.path.getsize(out) if os.path.exists(out) else 0
    raw["encode"], enc_summary = encode_check(r, size, ENCODE_FRAMES, soc_choices(ctx["soc"])[0]["encoder_hal"])
    parts.append(enc_summary)
    ok &= raw["encode"]["ok"]

    if not clips:
        ok = False
        parts.append("no test clips in the image")
    mpp_version = re.search(r"mpp version: (.*)", output_of(info))
    parts.append(f"mpp_info_test {'ok' if info['rc'] == 0 else failure_of(info)}"
                 + (f" (mpp version: {mpp_version.group(1).strip()[:80]})" if mpp_version else ""))

    # MPP picks its SoC table only from /proc/device-tree/compatible; without
    # it the HALs for this SoC do not register, which otherwise reads as a
    # plain decode failure.
    dt_access, _ = device_tree_access()
    detected = sorted({d["soc_matched"] for d in raw["decode"] if d.get("soc_matched")})
    fell_back = any(d["mpp_soc_log"]["default_chip_info"] or d["mpp_soc_log"]["no_soc_match"] is not None
                    or d["mpp_soc_log"]["compatible_open_error"] for d in raw["decode"])
    raw["mpp_soc"] = {"detected": detected, "default_table": fell_back, "device_tree_readable": dt_access}
    parts.insert(0, f"MPP detected SoC {', '.join(detected)}" if detected
                 else "MPP detected no SoC" + (" and used its default table" if fell_back else ""))
    if not ok and not dt_access["/proc/device-tree/compatible"]:
        parts.insert(0, "cause: /proc/device-tree/compatible is not readable in the container, so MPP falls back "
                        "to its default SoC table, under which no decoder for this SoC is registered"
                     + (" (only /device-tree is provided)" if dt_access["/device-tree/compatible"] else ""))
    return ("pass" if ok else "fail"), "; ".join(parts), raw


def parse_rgaprobe(text):
    # rgaprobe's own lines start with case=; anything else is librga's log.
    results, log_lines = [], []
    for line in text.splitlines():
        if not line.startswith("case="):
            if line.strip():
                log_lines.append(line)
            continue
        head, _, detail = line.partition(" detail=")
        entry = dict(kv.split("=", 1) for kv in head.split() if "=" in kv)
        entry["ok"] = entry.get("ok") == "1"
        for k in ("ms", "min", "max", "mae", "content_mae", "other_range_mae"):
            if k in entry:
                entry[k] = float(entry[k])
        for k in ("n", "maxerr", "content_maxerr", "pad_bad", "jobs", "failures", "checksums"):
            if k in entry:
                entry[k] = int(entry[k])
        entry["detail"] = detail
        results.append(entry)
    return results, log_lines


# Only heap paths: rgaprobe also says "open /dev/rga: No such file ..." for every
# buffer type when the RGA device itself is missing, which is a failure.
def heap_absent(result):
    return bool(result) and not result["ok"] and re.fullmatch(r"open /dev/(dma_heap|rk_dma_heap)/\S+: No such file or directory", result["detail"]) is not None


def rga_verdict(results):
    by = {(r["case"], r.get("buf")): r for r in results}
    problems = []
    version = by.get(("version", "-"))
    if not version or not version["ok"]:
        problems.append("RGA version not readable" + (f": {version['detail']}" if version else ""))
    buffers = sorted({r["buf"] for r in results if r["case"] in RGA_CASES})
    summary = {}
    for buf in buffers:
        failed = [c for c in RGA_CASES if not by.get((c, buf), {}).get("ok")]
        summary[buf] = "all cases ok" if not failed else "failed: " + ", ".join(failed)
        # A heap the kernel does not create (rk-dma-heap-cma on some boards) is
        # a fact about the board, not a failed case.
        if failed and all(heap_absent(by.get((c, buf))) for c in RGA_CASES):
            summary[buf] = "not present"
    for buf in RGA_GATING_BUFFERS:
        failed = [c for c in RGA_CASES if not by.get((c, buf), {}).get("ok")]
        if failed:
            reasons = {}
            for c in failed:
                reasons.setdefault(by[(c, buf)]["detail"] if (c, buf) in by else "no result", []).append(c)
            problems.append(f"{buf} buffers: " + "; ".join(f"{', '.join(cs)} ({why})" for why, cs in reasons.items()))
    stress = by.get(("stress", "system"))
    if not stress:
        problems.append("no stress result")
    elif not stress["ok"]:
        problems.append(f"stress: {stress.get('failures')} failed jobs, {stress.get('checksums')} distinct checksums"
                        f" ({stress['detail']})")
    return problems, summary


def probe_rga(ctx):
    # Results go to their own file because librga prints its log to stdout.
    try:
        os.unlink(RGA_RESULTS)
    except OSError:
        pass
    r = run(["/usr/local/bin/rgaprobe", "--results", RGA_RESULTS], timeout=240)
    results, _ = parse_rgaprobe(read_text(RGA_RESULTS, limit=1 << 24) or "")
    _, log_lines = parse_rgaprobe(r.get("stdout", ""))
    raw = {
        "node": describe_node("/dev/rga"),
        "files": {p: read_text(p) for p in (
            "/sys/kernel/debug/rkrga/driver_version", "/sys/kernel/debug/rkrga/hardware",
            "/proc/rkrga/driver_version", "/proc/rkrga/hardware")},
        "results": results,
        # librga logs its reasons for refusing a buffer (e.g. memory above
        # 4 GiB) itself; keep them verbatim.
        "librga_log": head_tail(log_lines + (r.get("stderr") or "").splitlines()),
        "process": {k: v for k, v in r.items() if k not in ("stdout", "stderr")},
    }
    problems, summary = rga_verdict(results)
    raw["buffers"] = summary
    if r["rc"] not in (0, 1):
        problems.insert(0, f"rgaprobe {failure_of(r)}")

    by = {(x["case"], x.get("buf")): x for x in results}
    parts = []
    if (v := by.get(("version", "-"))) and v["ok"]:
        parts.append(f"version {v['detail']}")
    head = by.get(("letterbox-bt601-limited", "system"))
    if head and head["ok"]:
        parts.append(f"bt601-limited letterbox on system heap {head.get('ms')} ms "
                     f"(content mae {head.get('content_mae')}, pad errors {head.get('pad_bad')})")
    if cpu := by.get(("cpu-letterbox", "cpu")):
        parts.append(f"CPU reference letterbox {cpu.get('ms')} ms")
    if stress := by.get(("stress", "system")):
        parts.append(f"stress {stress.get('jobs')} jobs, {stress.get('failures')} failed, "
                     f"{stress.get('checksums')} distinct checksums")
    parts.append("buffers: " + "; ".join(f"{b} {s}" for b, s in summary.items()))
    if problems:
        return "fail", "; ".join(problems + parts), raw
    return "pass", "; ".join(parts), raw


PROBES = {"environment": probe_environment, "npu": probe_npu, "gpu": probe_gpu, "vpu": probe_vpu,
          "rga": probe_rga}


def head_tail(lines, n=LOG_LINES):
    if len(lines) <= 2 * n:
        return list(lines)
    return lines[:n] + [f"... {len(lines) - 2 * n} lines omitted ..."] + lines[-n:]


def bound_for_report(value):
    """Shortens captured process output inside raw, keeping its head and tail."""
    if isinstance(value, dict):
        return {k: (bound_text(v) if k in ("stdout", "stderr") and isinstance(v, str) else bound_for_report(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [bound_for_report(v) for v in value]
    return value


def bound_text(text):
    if len(text) <= OUTPUT_LIMIT:
        return text
    half = OUTPUT_LIMIT // 2
    return text[:half] + f"\n... {len(text) - 2 * half} characters omitted ...\n" + text[-half:]


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
            "seconds": round(time.monotonic() - t0, 2), "raw": bound_for_report(raw)}


def clamp_seconds(value, low, high, default):
    """Returns (seconds, note); never raises, whatever JSON produced."""
    shown = repr(value) if len(repr(value)) <= 40 else repr(value)[:37] + "..."
    # bool is an int subclass and NaN/inf are floats; none is a duration.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default, f"{shown} is not a number; using the default {default}"
    if isinstance(value, float) and math.isnan(value):
        return default, f"{shown} is not a number; using the default {default}"
    # Compare before converting: int(1e308) is fine, but a 400-digit int
    # cannot become a float, and min/max on Python ints never overflow.
    if value < low or value > high:
        clamped = low if value < low else high
        return clamped, f"{shown} is outside {low}-{high}; clamped to {clamped}"
    return int(value), None


def parse_options(text):
    """Never raises: anything unusable falls back to the default, with a note saying why."""
    opts = json.loads(json.dumps(DEFAULT_OPTIONS))
    notes = []
    try:
        given = json.loads(text)
    except ValueError as e:
        return opts, [f"options.json not used: {e}"]
    if not isinstance(given, dict):
        return opts, [f"options.json not used: top level is {type(given).__name__}, not an object"]

    probes = given.get("probes", opts["probes"])
    if isinstance(probes, list):
        unknown = [p for p in probes if p not in ALL_PROBES]
        if unknown:
            notes.append(f"ignored unknown probes {unknown!r}")
        opts["probes"] = [p for p in ALL_PROBES if p in probes]
    else:
        notes.append(f"probes {probes!r} is not a list; using the default")

    seconds = given.get("npu_loop_seconds", opts["npu_loop_seconds"])
    value, note = clamp_seconds(seconds, 0, 120, opts["npu_loop_seconds"])
    opts["npu_loop_seconds"] = value
    if note:
        notes.append(f"npu_loop_seconds {note}")

    fetch = given.get("rknn_fetch", opts["rknn_fetch"])
    if isinstance(fetch, bool):
        opts["rknn_fetch"] = fetch
    else:
        notes.append(f"rknn_fetch {fetch!r} is not a boolean; using the default")
    return opts, notes


def load_options():
    try:
        with open(os.path.join(DATA, "options.json")) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return json.loads(json.dumps(DEFAULT_OPTIONS)), [f"options.json not used: {e}"]
    return parse_options(text)


def overall_status(probes, unsupported):
    if unsupported:
        return "unsupported SoC"
    if any(p["status"] == "fail" for p in probes):
        return "fail"
    gating = [p for p in probes if p["name"] in GATING_PROBES]
    if not gating:
        return "no gating probe ran"
    return "pass" if all(p["status"] == "pass" for p in gating) else "fail"


def write_report(report):
    body = finite.dumps(report)
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
                body = finite.dumps(REPORT, indent=2).encode()
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


def publish():
    """Snapshots the live report to disk, so the file never shows an earlier run's result."""
    with REPORT_LOCK:
        snapshot = json.loads(finite.dumps(REPORT))
    return snapshot, write_report(snapshot)


def fetch_rknn(allow_download, model_entry):
    # In a child process with a hard kill, so a stalled DNS lookup or TLS
    # handshake cannot hold the other probes beyond the fetch budget.
    r = run([sys.executable, rknn_fetch.__file__, RUNTIME_SOURCES, "1" if allow_download else "0",
             str(RKNN_FETCH_SECONDS), model_entry], timeout=RKNN_FETCH_SECONDS + 15)
    found = [line[len("FETCH_JSON:"):] for line in r.get("stdout", "").splitlines() if line.startswith("FETCH_JSON:")]
    if found:
        return json.loads(found[-1])
    return {"ok": False, "seconds": r.get("seconds"), "error": f"fetch process {failure_of(r)}",
            "output": bound_text((r.get("stdout") or "") + (r.get("stderr") or ""))}


def run_all():
    options, notes = load_options()
    soc = soc_info()
    forced = os.environ.get("ROCKCHIP_PROBE_IGNORE_SOC") == "1"
    t0 = time.monotonic()
    with REPORT_LOCK:
        choice, guessed = soc_choices(soc)
        REPORT.update({"options": options, "option_notes": notes,
                       "soc": {**soc, "check_ignored": forced,
                               "per_soc": {"table_entry": choice["name"], "model_file": choice["model_file"],
                                           "encoder_hal": choice["encoder_hal"], "guessed": guessed}}})
    publish()

    ctx = {"options": options, "soc": soc}
    unsupported = not soc["supported"] and not forced
    if "npu" in options["probes"] and not unsupported:
        log("fetching the RKNN files" if options["rknn_fetch"] else "checking the RKNN cache (rknn_fetch is false)")
        ctx["rknn"] = fetch_rknn(options["rknn_fetch"], soc_choices(soc)[0]["model_entry"])
        log(f"RKNN files: {'ok' if ctx['rknn'].get('ok') else rknn_unavailable(ctx['rknn'])} "
            f"in {ctx['rknn'].get('seconds')} s")
        with REPORT_LOCK:
            REPORT["rknn_files"] = ctx["rknn"]
        publish()
    reason = "unsupported SoC: " + (", ".join(soc["compatible"]) or "device tree not readable")
    for name in options["probes"]:
        if unsupported and name != "environment":
            result = {"name": name, "status": "skipped", "detail": reason, "started_at": now(),
                      "finished_at": now(), "seconds": 0, "raw": {}}
        else:
            result = run_probe(name, ctx)
        with REPORT_LOCK:
            REPORT["probes"].append(result)
        publish()

    with REPORT_LOCK:
        counts = {s: sum(1 for p in REPORT["probes"] if p["status"] == s)
                  for s in ("pass", "fail", "skipped", "reported")}
        overall = overall_status(REPORT["probes"], unsupported)
        REPORT.update({"status": "done", "finished_at": now(), "seconds": round(time.monotonic() - t0, 2),
                       "summary": {"overall": overall, **counts, **({"reason": reason} if unsupported else {})}})
    return overall, counts


def main():
    # PID 1 ignores signals that have no handler, so `stop` would wait for the kill timeout.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))

    with REPORT_LOCK:
        REPORT.clear()
        REPORT.update({"app": "rockchip_probe", "version": app_version(), "status": "running",
                       "started_at": now(), "probes": []})
    # Replace any report a previous run left in /data before anything can fail.
    publish()

    server = ThreadingHTTPServer(("", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"serving the report on :{PORT}")

    try:
        overall, counts = run_all()
        log(f"done: {overall} {counts}")
    except Exception as e:  # end in a served report, never a dead container stuck at "running"
        with REPORT_LOCK:
            REPORT.update({"status": "error", "error": repr(e), "finished_at": now()})
        log(f"error: {e!r}")
    final, path = publish()
    log(f"report at {path} and http://<host>:<mapped port>/report.json")
    print(finite.dumps(final), flush=True)

    while True:
        signal.pause()


if __name__ == "__main__":
    main()
