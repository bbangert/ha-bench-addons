# Rockchip Probe

Checks, from inside an app container, that the NPU, GPU, video codecs and RGA
2D accelerator of a
Rockchip RK3566 or RK3568 work with the userspace bundled in this image. Built
for the Home Assistant Green (RK3566, vendor 6.1 kernel).

**Test bench only.** The image bundles unmodified vendor binaries (see
[Licences](#licences)); this repository offers it for bench testing and
nothing else. It has no Supervisor or Core API access.

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
`npu_loop_seconds` that is not a finite number) falls back to the defaults;
`option_notes` in the report says what was ignored and why.

If `/proc/device-tree/compatible` does not name `rockchip,rk3566` or
`rockchip,rk3568`, only `environment` runs, the others are `skipped`, and
`summary.overall` is `unsupported SoC` with the compatible string.

## What each probe checks

| Probe | Pass when |
|---|---|
| `environment` | The device tree `compatible` and `model` are readable. Also inventories the device nodes (type, major:minor, mode, whether they open read-write), the kernel version and the versions of everything in the image. |
| `npu` | The official rknn-toolkit-lite2 resnet18 example runs on the NPU and its TOP5 classes match the example's published result (`[812] space shuttle` first). Records the runtime and kernel driver versions (`0.9.8` is expected; another version is reported, not failed), then loops inference for `npu_loop_seconds` and records throughput; the loop fails the probe if it completes no inference or if the TOP1 class ever changes. NPU load is sampled from `/proc/rknpu/load` or `/sys/kernel/debug/rknpu/load` if either is visible in the container, otherwise recorded as not available. |
| `gpu` | `eglprobe` gets a GLES 3 context through the GBM platform on the `rockchip` DRM card, reports `OpenGL ES 3.x` and a `Mali-G52` renderer, completes an FBO and reads back the clear colour `255,0,0,255`. The surfaceless (`EGL_PLATFORM_SURFACELESS_MESA`) and default-display (`eglGetDisplay(EGL_DEFAULT_DISPLAY)`) paths are tried and recorded with `"gating": false`; they never affect the result. On a Green, surfaceless failed and the default display worked; GBM is the path the probe relies on. |
| `vpu` | `mpi_dec_test` decodes the H.264 and H.265 test clips through a hardware HAL that MPP names in its log, producing the expected frame count with no error frames; `mpi_enc_test` encodes exactly the 30 requested frames through a hardware HAL named in its log (on a Green, `hal_h264e_vepu541`) into a non-empty H.264 stream. `mpp_info_test` output is recorded. |
| `rga` | `rgaprobe` (below) reads the library and driver versions and runs copy, resize, colour conversion, the letterbox, crop+rotate+flip and fill, each checked against a CPU reference. Passes when the version is readable and every case passes with both plain user-memory buffers and dma-bufs from `/dev/dma_heap/system`. |

### RGA

RGA is the SoC's 2D engine and its only hardware scaler, so it sits between
the decoder and the NPU: decoded NV12 frames are scaled and converted to the
detector's RGB input on it. `rgaprobe` ([`rgaprobe.cpp`](rgaprobe.cpp)) drives
it through librga's im2d API and prints one `case=… buf=… ok=… ms=… detail=…`
line per result:

| Case | What is checked |
|---|---|
| `version`, `capabilities` | `querystring(RGA_VERSION)` must be readable; `RGA_ALL` (limits, formats, scale range) is recorded |
| `copy` | RGBA 1280x720 copy, byte-exact |
| `resize-nv12`, `resize-rgba` | NV12 1920x1080 to 640x360, RGBA 1920x1080 to 640x640; mean absolute error at most 6 against the closest of a bilinear, area and nearest CPU reference (MAE and max error recorded) |
| `cvtcolor` | NV12 to RGB888 at 1280x720 in BT.601 limited and full range; passes if either matches its CPU reference within MAE 3, and says which |
| `letterbox` | NV12 1920x1080 to a 640x640 RGB888 detector input: 640x360 content at y=140, pad 114 written by `imfill` and required exact, content within MAE 6 |
| `crop-rotate-flip` | a 320x240 crop rotated 90° and flipped in one job, byte-exact |
| `fill` | `imfill` of a rect: uniform inside, nothing changed outside |
| `cpu-letterbox` | the same letterbox on the CPU (the tool's plain reference code), for scale |
| `stress` | 500 consecutive letterbox jobs on dma-heap buffers; records failures and how many distinct output checksums appeared (one is expected); reported, not gated |

The input is a structured pattern (gradients, colour bars, hard lines and a
checker block), not noise, so a wrong kernel, offset or channel order stands
out. Each case runs once to be checked, then 50 more times for mean/min/max
timing. All cases run for every buffer type: user memory (`va`) and dma-bufs
from `system`, `system-uncached`, `reserved` and `rk-dma-heap-cma`. Only `va`
and `system` gate; the others are reported per heap. RGA2 addresses only
memory below 4 GiB; the probe reports per buffer type whether a job was
accepted, and keeps librga's own error text verbatim in `raw.librga_log`.
`rgaprobe --selftest` checks the pattern generator and CPU references without
RGA, and runs during the image build.

## Device access

`config.yaml` asks for `devicetree: true`, because MPP selects its codec table
by reading `/proc/device-tree/compatible`, and lists the DRM card and render
nodes, `/dev/mali0`, `/dev/mpp_service`, `/dev/rga` and the DMA heaps under
`devices:`. Nodes a kernel does not create are skipped. The process runs as
root because these nodes are root-only on the Green.

The libmali in the image is DDK `g29p1`. The kernel's Mali driver must be the
same version; a kernel that moves to another DDK needs a matching blob here.

## What is in the image

Debian 12 (bookworm) slim, pinned by digest, with Python 3.11, NumPy, Pillow,
psutil, ruamel.yaml and libdrm from Debian. Everything else is listed in
[`SOURCES`](SOURCES), pinned by commit or tag and sha256; the build fails on
any mismatch.

| Component | Version | Source | Licence |
|---|---|---|---|
| `librknnrt.so`, rknn-toolkit-lite2 (cp311 wheel), resnet18 example model, image and labels | 2.3.2 | [airockchip/rknn-toolkit2](https://github.com/airockchip/rknn-toolkit2/tree/v2.3.2) @ `42aa1d4` | Rockchip, [`licenses/rknn-toolkit2-LICENSE.txt`](licenses/rknn-toolkit2-LICENSE.txt) |
| `libmali-bifrost-g52-g29p1-gles.so` (EGL, GLES, GBM) | g29p1-12eac0 | [JeffyCN/mirrors](https://github.com/JeffyCN/mirrors/tree/bf621d15f009509557d6b28f81427651695cac20) branch `libmali-next` @ `bf621d1` | Arm EULA, [`licenses/libmali-END_USER_LICENCE_AGREEMENT.txt`](licenses/libmali-END_USER_LICENCE_AGREEMENT.txt) |
| MPP (`librockchip_mpp`, `mpp_info_test`, `mpi_dec_test`, `mpi_enc_test`), built from source | 1.1.0 | [rockchip-linux/mpp](https://github.com/rockchip-linux/mpp/tree/1.1.0) @ `c08762e` | Apache-2.0 or MIT, [`licenses/`](licenses/) |
| librga (`librga.so`, im2d API), built from source | API 1.10.5_[11] | [JeffyCN/mirrors](https://github.com/JeffyCN/mirrors/tree/57a1067a246c71fa6c9a355d1668884fda155dd5) branch `linux-rga-multi` @ `57a1067` | Apache-2.0, [`licenses/librga-COPYING.txt`](licenses/librga-COPYING.txt); bundled libdrm header notices in [`licenses/librga-libdrm-headers-NOTICE.txt`](licenses/librga-libdrm-headers-NOTICE.txt) |
| `eglprobe`, `rgaprobe` | — | [`eglprobe.c`](eglprobe.c), [`rgaprobe.cpp`](rgaprobe.cpp), this repository | — |
| Test clips, 1280x720, 150 frames, H.264 and H.265 | — | generated during the build from FFmpeg's `testsrc` | no third-party media |

The licence texts are also in the image under `/licenses`, and the build checks
that they are byte-identical to the upstream copies of the pinned inputs.

### Licences

- **RKNN.** All RKNN files come from the v2.3.2 tag, whose `LICENSE` is a
  Rockchip copyright statement: the software is provided "as is", without
  warranty. It states no use restriction and no express grant to redistribute.
  The files are redistributed here unmodified, without such a grant, for bench
  testing.
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
  --security-opt unmask=/sys/firmware \
  --mount type=bind,src=/sys/firmware/devicetree/base,dst=/device-tree,ro \
  ghcr.io/bbangert/ha-bench-addons/aarch64-rockchip-probe:0.1.0
```

The unmask is what lets `/proc/device-tree` resolve, which MPP needs; the bind
alone satisfies only the probe's own SoC check.

`ROCKCHIP_PROBE_IGNORE_SOC=1` runs every probe even when the SoC check fails,
which is only useful for checking that the probes fail cleanly on other
hardware.
