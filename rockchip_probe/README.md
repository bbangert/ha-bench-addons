# Rockchip Probe

Checks, from inside an app container, that the NPU, GPU and video codecs of a
Rockchip RK3566 or RK3568 work with the userspace bundled in this image. Built
for the Home Assistant Green (RK3566, vendor 6.1 kernel).

**Test bench only.** The image bundles vendor binaries whose licences limit
what they may be used for (see [Licences](#licences)). It has no Supervisor or
Core API access.

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
is `fail` if any probe failed. Every subprocess has a timeout, so a wedged
driver produces a failure, not a hung run.

If `/proc/device-tree/compatible` does not name `rockchip,rk3566` or
`rockchip,rk3568`, only `environment` runs, the others are `skipped`, and
`summary.overall` is `unsupported SoC` with the compatible string.

## What each probe checks

| Probe | Pass when |
|---|---|
| `environment` | The device tree `compatible` and `model` are readable. Also inventories the device nodes (type, major:minor, mode, whether they open read-write), the kernel version and the versions of everything in the image. |
| `npu` | The official rknn-toolkit-lite2 resnet18 example runs on the NPU and its TOP5 classes match the example's published result (`[812] space shuttle` first). Records the runtime and kernel driver versions (`0.9.8` is expected; another version is reported, not failed), then loops inference for `npu_loop_seconds` and records throughput. NPU load is sampled from `/proc/rknpu/load` or `/sys/kernel/debug/rknpu/load` if either is visible in the container, otherwise recorded as not available. |
| `gpu` | `eglprobe` gets a GLES 3 context through the GBM platform on the `rockchip` DRM card, reports `OpenGL ES 3.x` and a `Mali-G52` renderer, completes an FBO and reads back the clear colour `255,0,0,255`. The surfaceless (`EGL_PLATFORM_SURFACELESS_MESA`) and default-display paths are tried and recorded too; with this libmali they are expected to fail and do not affect the result, which tells a consumer it has to use GBM. |
| `vpu` | `mpi_dec_test` decodes the H.264 and H.265 test clips through a hardware HAL that MPP names in its log, producing the expected frame count with no error frames; `mpi_enc_test` produces a non-empty H.264 stream. `mpp_info_test` output is recorded. |
| `rga` | Never gated (`reported`): whether `/dev/rga` is present and openable, and the driver version if visible. |

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
| `eglprobe` | — | [`eglprobe.c`](eglprobe.c), this repository | — |
| Test clips, 1280x720, 150 frames, H.264 and H.265 | — | generated during the build from FFmpeg's `testsrc` | no third-party media |

The licence texts are also in the image under `/licenses`, and the build checks
that they are byte-identical to the upstream copies of the pinned inputs.

### Licences

- **RKNN.** The v2.3.2 tag ships a Rockchip copyright statement that provides
  the software "as is" without warranty and grants no further terms. Later
  releases (v2.5.0) replace it with the RKNN SDK License, which permits use and
  redistribution solely for the design, development and testing of
  applications compatible with Rockchip products. This app exists for exactly
  that kind of testing and should not be used for anything else.
- **libmali.** Arm's end user licence permits distributing the whole of the
  software for use with Mali-based products, provided notices are kept and a
  copy of the licence accompanies it.
- **MPP** is dual-licensed Apache-2.0 or MIT.

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
