# Rockchip Probe

Checks, from inside an app container, that the NPU, GPU, video codecs and RGA
2D accelerator of a
Rockchip RK3566, RK3568 or RK3576 work with the userspace bundled in this
image. Built for the Home Assistant Green (RK3566, vendor 6.1 kernel).

| SoC | Board | Status |
|---|---|---|
| RK3566 | Home Assistant Green | Run on hardware |
| RK3576 | Radxa ROCK 4D | Run on hardware (4 GiB board, vendor 6.1 kernel); a 6 GiB board has not been run |

See [Results on hardware](#results-on-hardware).

**Test bench only.** The image bundles Arm's Mali userspace driver, and the
app downloads Rockchip's RKNN runtime and example at first start (see
[Licences](#licences)); this repository offers it for bench testing and
nothing else. It has no Supervisor or Core API access.

**The first start needs internet access** (to `raw.githubusercontent.com`),
unless `/data/rknn` is already seeded; see [RKNN files](#rknn-files).

## Running it

Install *Rockchip Probe* and start it. On start it runs every selected probe
once, then keeps serving the result until stopped:

- the last line of the app log is the whole report as one JSON object;
- it is written to `/data/report.json`;
- `GET http://<host>:4010/report.json` returns it (`/healthz` answers `ok`).
  While probes are still running, the same URL returns the partial report with
  `"status": "running"`.

A full run takes well under a minute plus the NPU loop. To run again, restart
the app.

| Option | Default | Meaning |
|---|---|---|
| `probes` | all five | Which of `environment`, `npu`, `gpu`, `vpu`, `rga` to run |
| `npu_loop_seconds` | `10` | How long the NPU inference loop runs (0–120; 0 skips the loop) |
| `rknn_fetch` | `true` | Download missing RKNN files at start; `false` uses only what is already in `/data/rknn` |

### RKNN files

The image does not contain Rockchip's RKNN files. When the `npu` probe is
selected, the app downloads them on start from the pinned upstream URLs in
[`RUNTIME_SOURCES`](RUNTIME_SOURCES) (airockchip/rknn-toolkit2 at commit
`42aa1d4`, the v2.3.2 tag): `librknnrt.so`, the rknn-toolkit-lite2 cp311
wheel, the resnet18 example's model for the detected SoC (the other SoC's model
is not fetched), and its test image and labels. Each download
goes to a temporary name and is kept only if its sha256 matches; nothing is
imported or loaded before that. Files are kept in `/data/rknn` and re-checked
on every start; a file that fails the check is deleted and fetched again. The
wheel is unzipped (never pip-installed, with a path-traversal check) into
`/data/rknn/site-<hash>`, rebuilt from the verified wheel on every start
rather than trusting an earlier tree, and the image's `/usr/lib/librknnrt.so` is a symlink
that resolves into the verified copy. Downloads retry with backoff and share a
120-second deadline that is checked on every socket read; a body larger than
the size pinned in `RUNTIME_SOURCES` is refused, and the whole fetch runs in a
child process that is killed 15 seconds past the deadline, so a stalled name
lookup or handshake cannot hold up the other probes. If `/data` is not writable the files go to a temporary
directory for that run, and the report says so. If `/data` is mounted
`noexec`, it stays the verified download cache, and hash-checked copies of the
runtime and the unpacked wheel are loaded from the container's own filesystem
instead (`exec_dir` and `note` in the report).

The report lists every file with its URL, expected sha256 and whether it was
`cached`, `downloaded` or `failed` (with the error), plus the fetch time. A
self-check after the fetch imports `rknnlite`, loads the runtime and confirms
the decoded test image is the one the official example feeds the model. If
any file is missing, the `npu` probe fails with `RKNN files not available: …`
and the other probes run as usual. To run offline, copy the five files into
`/data/rknn` and set `rknn_fetch: false`; the model is `resnet18_for_rk3566_rk3568.rknn` or
`resnet18_for_rk3576.rknn`.

Every probe reports `{name, status, detail, raw, started_at, finished_at}`
with `status` one of `pass`, `fail`, `skipped` or `reported`. `summary.overall`
is `fail` if any probe failed, `pass` only if every selected `npu`, `gpu`,
`vpu` and `rga` probe passed and at least one of them ran, and `no gating probe
ran` when none of those four was selected. Every subprocess has a timeout, so a wedged
driver produces a failure, not a hung run. `/data/report.json` is rewritten
with the `running` report at start and after each probe, so it never shows an
earlier run. If the probe itself hits an unexpected error, the report ends with
`"status": "error"` and the exception text.

An unusable `options.json` (not an object, a non-list `probes`, a
`npu_loop_seconds` that is not a number) falls back to the defaults, and a
`npu_loop_seconds` outside 0–120, however large, is clamped; `option_notes` in
the report says what was ignored or clamped and why.

If `/proc/device-tree/compatible` does not name `rockchip,rk3566`,
`rockchip,rk3568` or `rockchip,rk3576`, only `environment` runs, the others are
`skipped`, and `summary.overall` is `unsupported SoC` with the compatible string.

### Per-SoC choices

The detected SoC picks the following, and the report records them under
`soc.per_soc` (and, in full, in the `environment` probe's `raw.per_soc`):

| | RK3566 / RK3568 | RK3576 |
|---|---|---|
| RKNN model | `resnet18_for_rk3566_rk3568.rknn` | `resnet18_for_rk3576.rknn` |
| Expected H.264 encoder HAL | `hal_h264e_vepu541` | `hal_h264e_vepu510` |
| Expected NPU TOP5 | `[812, 404, 657, 466, 895]` | `[812, 404, 657, 833, 466]` |

The RK3566 list is the one published in the example's README, which notes that
other platforms and driver versions "may have slightly different results". The
RK3576 list was measured on a ROCK 4D, where 833 ranks fifth ahead of 466 and
895 drops out. The gate stays strict for both: TOP1 must be 812 and the five
classes must be exactly the expected set.

Everything else (the libmali blob, MPP, librga, the RKNN runtime and wheel) is
shared. A bound encoder HAL other than the expected one is reported, not
failed; the frame count and a non-empty stream still gate. With
`ROCKCHIP_PROBE_IGNORE_SOC=1` on an unlisted SoC the RK3566 choices are used and
`per_soc.guessed` is true.

## What each probe checks

| Probe | Pass when |
|---|---|
| `environment` | The device tree `compatible` and `model` are readable. Also inventories the device nodes (type, major:minor, mode, whether they open read-write), the kernel version and the versions of everything in the image. |
| `npu` | The official rknn-toolkit-lite2 resnet18 example runs on the NPU and its TOP5 classes match the expected list for the SoC (`[812] space shuttle` first; see [Per-SoC choices](#per-soc-choices)). Records the runtime and kernel driver versions (`0.9.8` is expected; another version is reported, not failed), then loops inference for `npu_loop_seconds` and records throughput; the loop fails the probe if it completes no inference or if the TOP1 class ever changes. NPU load is sampled from `/proc/rknpu/load` or `/sys/kernel/debug/rknpu/load` if either is visible in the container, otherwise recorded as not available. |
| `gpu` | `eglprobe` gets a GLES 3 context through the GBM platform on the `rockchip` DRM card, reports `OpenGL ES 3.x` and a `Mali-G52` renderer, completes an FBO and reads back the clear colour `255,0,0,255`. The surfaceless (`EGL_PLATFORM_SURFACELESS_MESA`) and default-display (`eglGetDisplay(EGL_DEFAULT_DISPLAY)`) paths are tried and recorded with `"gating": false`; they never affect the result. On a Green, surfaceless failed and the default display worked; GBM is the path the probe relies on. |
| `vpu` | `mpi_dec_test` decodes the H.264 and H.265 test clips through a hardware HAL that MPP names in its log, producing the expected frame count with no error frames; `mpi_enc_test` encodes exactly the 30 requested frames through a hardware HAL named in its log (on a Green, `hal_h264e_vepu541`; an RK3576 is expected to use `hal_h264e_vepu510`, see [Per-SoC choices](#per-soc-choices)) into a non-empty H.264 stream. `mpp_info_test` output is recorded. |
| `rga` | `rgaprobe` (below) reads the library and driver versions and runs copy, resize, colour conversion, the letterbox, crop+rotate+flip and fill, each checked against a CPU reference. Passes when the version is readable, every case passes with dma-bufs from `/dev/dma_heap/system`, and the 500-job stress run has no failures and one output checksum. |

### RGA

RGA is the SoC's 2D engine and its only hardware scaler, so it sits between
the decoder and the NPU: decoded NV12 frames are scaled and converted to the
detector's RGB input on it. `rgaprobe` ([`rgaprobe.cpp`](rgaprobe.cpp)) drives
it through librga's im2d API and writes one `case=… buf=… ok=… ms=… detail=…`
line per result to a results file of its own (`--results`), because librga
prints its log to stdout:

| Case | What is checked |
|---|---|
| `version`, `capabilities` | `querystring(RGA_VERSION)` must be readable; `RGA_ALL` (limits, formats, scale range) is recorded |
| `copy` | RGBA 1280x720 copy, byte-exact |
| `resize-nv12`, `resize-rgba` | NV12 1920x1080 to 640x360, RGBA 1920x1080 to 640x640; mean absolute error at most 6 against the closest of a bilinear, area and nearest CPU reference (MAE and max error recorded) |
| `cvtcolor-bt601-limited`, `cvtcolor-bt601-full` | NV12 to RGB888 at 1280x720 with the colour range requested explicitly; the output must be within MAE 3 of the CPU reference for the requested range and closer to it than to the other range |
| `letterbox-bt601-limited`, `letterbox-bt601-full` | NV12 1920x1080 to a 640x640 RGB888 detector input, range requested explicitly: 640x360 content at y=140 within MAE 6 of the requested range (and closer to it than to the other), and the pad, written by `imfill`, exactly 114,114,114; content MAE and wrong pad bytes are reported separately |
| `crop-rotate-flip` | a 320x240 crop rotated 90° and flipped in one job, byte-exact |
| `fill` | `imfill` of an RGBA rect: uniform inside, nothing changed outside; reports the byte order the colour lands in |
| `fill-rgb888` | the same on RGB888; reports which three bytes of the 32-bit colour land in memory |
| `cpu-letterbox` | the same letterbox on the CPU (the tool's plain reference code), for scale |
| `stress` | 500 consecutive letterbox jobs on dma-heap `system` buffers, each into a zeroed output; the first output must pass the letterbox check, every later one must be identical to it, none may still be the zeroed buffer, and no job may fail |

The input is a structured pattern (gradients, colour bars, hard lines and a
checker block), not noise, so a wrong kernel, offset or channel order stands
out. Each case runs once to be checked, then 50 more times for mean/min/max
timing. All cases run for every buffer type: dma-bufs from `system`,
`system-uncached`, `reserved` and `rk-dma-heap-cma`, user memory imported with
`importbuffer_virtualaddr` (`va`), and user memory passed per job with
`wrapbuffer_virtualaddr` (`va-wrap`). Only `system` gates; the others are
reported per buffer type, and a heap the kernel does not create (the ROCK 4D has no
`rk-dma-heap-cma`) is reported as `not present`. On `va` and `va-wrap`, once one case fails the
remaining cases are reported as not attempted rather than submitted, since
each refused job makes librga print a long error table; a failing case is
never retried for timing. The report keeps the first and last 40 lines of
librga's log with a count of what was left out. RGA2 addresses only memory
below 4 GiB; the probe
reports per buffer type whether a job was accepted, and keeps librga's own
error text verbatim in `raw.librga_log`.

The pad colour is 114 in all four bytes of `imfill`'s colour argument. librga
and the driver hand that value to the hardware register unchanged, and which
byte lands in which channel depends on the output format (on RGBA the low byte
is R), so a uniform value is the only packing that is grey on every format.

On a Green, every dma-heap buffer type was accepted and user memory imported
with `importbuffer_virtualaddr` was not. Reading the driver, the per-job
`va-wrap` path reaches the same mapping code, so it is expected to be refused
too; the report shows whether it is.
The vendor kernel's RGA driver (multi-RGA 1.3.13) treats RK356x's RGA2 as a
core with its own page-table MMU and no IOMMU, maps user memory through the
DMA API without an IOMMU, and then refuses any mapping that comes back as more
than one segment (`dma_map_sg mapped more than one segment`), which is every
user buffer that is not physically contiguous. dma-bufs take a different path
that accepts scattered pages. No device-tree or kernel-config option changes
this, so consumers on this kernel should hand RGA dma-buf fds, such as dma-heap
allocations or the decoder's own frame buffers.
`rgaprobe --selftest` checks the pattern generator and CPU references without
RGA, and runs during the image build.

## Results on hardware

**Home Assistant Green (RK3566).** The RGA findings above come from it: every
dma-heap buffer type accepted, user memory refused.

**ROCK 4D (RK3576, 4 GiB, kernel 6.1.172), one run of 35.3 s, overall `fail`
only because of the NPU TOP5 list that this version corrects.**

| Probe | Result |
|---|---|
| `environment` | pass; SoC `rk3576` detected; `rk-dma-heap-cma` absent |
| `gpu` | pass; GBM on `card0`, OpenGL ES 3.2 (DDK g29p1), Mali-G52, clear colour read back |
| `vpu` | pass; MPP detected `rk3576`; H.264 150/150 frames via `h264d_vdpu383` (859 fps), H.265 150/150 via `h265d_vdpu383` (819 fps), encode 30/30 via `hal_h264e_vepu510` |
| `rga` | pass; RGA_2_PRO, API 1.10.5; letterbox 2.28 ms; stress 500 jobs, 0 failed, 1 checksum; every case ok on `system`, `system-uncached` and `reserved` buffers **and on user memory (`va`, `va-wrap`)**; `rk-dma-heap-cma` reported as not present |
| `npu` | inference works: TOP1 `[812] space shuttle` 0.999638, TOP5 `[812, 404, 657, 833, 466]`; librknnrt 2.3.2, driver 0.9.8; 1322 inferences in 10.01 s (132.13/s, 7.18 ms average) |

Unlike the RK3566, the RK3576's two RGA2 cores sit behind an IOMMU, so RGA jobs
on user memory are accepted there; the per-core page-table limitation described
under [RGA](#rga) is an RK356x one. The ROCK 4D's kernel creates no
`rk-dma-heap-cma` heap. Nothing here was measured on a 6 GiB board.

## Device access

`config.yaml` asks for `devicetree: true`. The probe's own SoC check reads
`/proc/device-tree/compatible` or, failing that, `/device-tree/compatible`, but
MPP reads only `/proc/device-tree/compatible` and has no other way to learn the
SoC. Where only `/device-tree` is provided, `environment` passes with a note
saying so, MPP falls back to its default SoC table and registers no decoder for
the RK3566, and `vpu` fails with that cause stated. `config.yaml` also lists the DRM card and render
nodes, `/dev/mali0`, `/dev/mpp_service`, `/dev/rga` and the DMA heaps under
`devices:`. Nodes a kernel does not create are skipped. The process runs as
root because these nodes are root-only on the Green.

The libmali in the image is DDK `g29p1`. The kernel's Mali driver must be the
same version; a kernel that moves to another DDK needs a matching blob here.

## What is in the image

Debian 12 (bookworm) slim, pinned by digest, with Python 3.11, NumPy, Pillow,
psutil, ruamel.yaml and libdrm from Debian. Everything else is listed in
[`SOURCES`](SOURCES), pinned by commit or tag and sha256; the build fails on
any mismatch. The RKNN files are not in the image (see [RKNN files](#rknn-files)).

| Component | Version | Source | Licence |
|---|---|---|---|
| `libmali-bifrost-g52-g29p1-gles.so` (EGL, GLES, GBM) | g29p1-12eac0 | [JeffyCN/mirrors](https://github.com/JeffyCN/mirrors/tree/bf621d15f009509557d6b28f81427651695cac20) branch `libmali-next` @ `bf621d1` | Arm EULA, [`licenses/libmali-END_USER_LICENCE_AGREEMENT.txt`](licenses/libmali-END_USER_LICENCE_AGREEMENT.txt) |
| MPP (`librockchip_mpp`, `mpp_info_test`, `mpi_dec_test`, `mpi_enc_test`), built from source | 1.1.0 | [rockchip-linux/mpp](https://github.com/rockchip-linux/mpp/tree/1.1.0) @ `c08762e` | Apache-2.0 or MIT, [`licenses/`](licenses/) |
| librga (`librga.so`, im2d API), built from source | API 1.10.5_[11] | [JeffyCN/mirrors](https://github.com/JeffyCN/mirrors/tree/57a1067a246c71fa6c9a355d1668884fda155dd5) branch `linux-rga-multi` @ `57a1067` | Apache-2.0, [`licenses/librga-COPYING.txt`](licenses/librga-COPYING.txt); bundled libdrm header notices in [`licenses/librga-libdrm-headers-NOTICE.txt`](licenses/librga-libdrm-headers-NOTICE.txt) |
| `eglprobe`, `rgaprobe` | — | [`eglprobe.c`](eglprobe.c), [`rgaprobe.cpp`](rgaprobe.cpp), this repository | — |
| Test clips, 1280x720, 150 frames, H.264 and H.265 | — | generated during the build from FFmpeg's `testsrc` | no third-party media |

The licence texts are also in the image under `/licenses`, and the build checks
that they are byte-identical to the upstream copies of the pinned inputs.

### Licences

- **RKNN.** Neither this repository nor the image redistributes the RKNN
  files; the app downloads them from the upstream repository at first start.
  They come under Rockchip's terms, which at the v2.3.2 tag are a copyright
  statement providing the software "as is", without warranty. That text is
  kept for reference in
  [`licenses-downloaded/rknn-toolkit2-LICENSE.txt`](licenses-downloaded/rknn-toolkit2-LICENSE.txt)
  and applies to the downloaded files only; it is not in the image.
- **libmali.** Arm's end user licence permits distributing the whole of the
  software for use with Mali-based products, provided notices are kept and a
  copy of the licence accompanies it.
- **MPP** is dual-licensed Apache-2.0 or MIT.
- **librga** is built from the source of Rockchip's Linux librga as published
  on the `linux-rga-multi` branch of JeffyCN/mirrors, whose `COPYING` is
  Apache-2.0 and whose sources carry Apache-2.0 headers. It compiles against
  copies of three libdrm headers it ships; two carry the MIT-style notices
  reproduced in `licenses/`, and `drm.h` carries no licence text in that copy.
  airockchip/librga, the other public home of librga, ships only prebuilt
  binaries (under the same Apache-2.0 `COPYING`), which this image does not use.

## Running the image without an app store

On the board, with the image loaded (`podman load -i …`):

```bash
podman run --rm -p 4010:8000 \
  --device /dev/dri/card0 --device /dev/dri/renderD128 \
  --device /dev/dri/card1 --device /dev/dri/renderD129 \
  --device /dev/mali0 --device /dev/mpp_service --device /dev/rga \
  --device /dev/dma_heap/system --device /dev/dma_heap/system-uncached \
  --device /dev/dma_heap/reserved \
  --security-opt unmask=/sys/firmware \
  --mount type=bind,src=/sys/firmware/devicetree/base,dst=/device-tree,ro \
  -v /var/lib/rockchip-probe-data:/data \
  ghcr.io/bbangert/ha-bench-addons/aarch64-rockchip-probe:0.2.0
```

The same command serves the Green and the ROCK 4D (the NPU is `card1`/`renderD129`
on both). Pass only `--device` paths that exist: podman refuses a missing one,
which is why `rk-dma-heap-cma` is not listed.

The unmask is what lets `/proc/device-tree` resolve, which MPP needs; the bind
alone satisfies only the probe's own SoC check.

`ROCKCHIP_PROBE_IGNORE_SOC=1` runs every probe even when the SoC check fails,
which is only useful for checking that the probes fail cleanly on other
hardware.
