# Debian rather than Alpine: librknnrt.so and libmali are glibc-only, and
# bookworm's Python 3.11 is one the rknn-toolkit-lite2 2.3.2 wheels are built for.
# Pinned by index digest (multi-arch); the probe itself is arm64-only.
ARG DEBIAN=debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

FROM ${DEBIAN} AS fetch
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY SOURCES ./
COPY licenses ./licenses-repo
RUN set -eu; \
    grep -v '^#' SOURCES | while read -r name version sha file url; do \
      [ -n "$name" ] || continue; \
      curl -fsSL --retry 3 -o "$file" "$url"; \
      echo "$sha  $file" | sha256sum -c -; \
    done
# The licence texts shipped in /licenses come from the repo; prove they are the
# upstream texts of exactly the inputs built here.
RUN set -eu; \
    mkdir mpp && tar -xzf mpp.tar.gz -C mpp --strip-components=1; \
    cmp rknn-toolkit2-LICENSE.txt licenses-repo/rknn-toolkit2-LICENSE.txt; \
    cmp libmali-END_USER_LICENCE_AGREEMENT.txt licenses-repo/libmali-END_USER_LICENCE_AGREEMENT.txt; \
    cmp mpp/LICENSES/Apache-2.0 licenses-repo/mpp-Apache-2.0.txt; \
    cmp mpp/LICENSES/MIT licenses-repo/mpp-MIT.txt

FROM ${DEBIAN} AS build
# Debian's Khronos/GBM headers are only for compiling eglprobe; the binary is
# linked against the libmali blob, which exports EGL, GLES and GBM itself.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config libdrm-dev libegl-dev libgles-dev libgbm-dev \
 && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /src/mpp /build/mpp
RUN cmake -S /build/mpp -B /build/mpp-out -DCMAKE_BUILD_TYPE=Release -DBUILD_TEST=ON \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
 && cmake --build /build/mpp-out -j"$(nproc)" \
 && DESTDIR=/out cmake --install /build/mpp-out \
 && rm -rf /out/usr/local/include /out/usr/local/lib/pkgconfig /out/usr/local/lib/*.a
COPY --from=fetch /src/libmali-bifrost-g52-g29p1-gles.so /build/mali/
COPY eglprobe.c /build/
RUN ln -s libmali-bifrost-g52-g29p1-gles.so /build/mali/libmali.so \
 && gcc -O2 -Wall -Wextra -o /out/usr/local/bin/eglprobe /build/eglprobe.c \
      $(pkg-config --cflags libdrm) -L/build/mali -lmali -ldrm

# Test clips are generated, not downloaded, so the image carries no
# third-party media. testsrc burns a frame counter into every frame.
FROM ${DEBIAN} AS clips
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /clips
RUN set -eu; \
    src="testsrc=size=1280x720:rate=30"; \
    ffmpeg -hide_banner -loglevel error -f lavfi -i "$src" -frames:v 150 \
      -c:v libx264 -pix_fmt yuv420p -f h264 testsrc-720p.h264; \
    ffmpeg -hide_banner -loglevel error -f lavfi -i "$src" -frames:v 150 \
      -c:v libx265 -x265-params log-level=error -pix_fmt yuv420p -f hevc testsrc-720p.h265; \
    count() { ffprobe -v error -count_frames -select_streams v:0 \
      -show_entries stream=nb_read_frames -of csv=p=0 "$1"; }; \
    printf '{"testsrc-720p.h264": {"codec": "h264", "width": 1280, "height": 720, "frames": %s},\n "testsrc-720p.h265": {"codec": "hevc", "width": 1280, "height": 720, "frames": %s}}\n' \
      "$(count testsrc-720p.h264)" "$(count testsrc-720p.h265)" > clips.json; \
    cat clips.json

FROM ${DEBIAN}
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-numpy python3-psutil python3-ruamel.yaml python3-pil libdrm2 \
 && rm -rf /var/lib/apt/lists/*

# rknnlite loads the runtime from this exact path.
COPY --from=fetch /src/librknnrt.so /usr/lib/librknnrt.so
# A wheel is a zip; extracting it avoids shipping pip. Its declared deps
# (numpy, psutil, ruamel.yaml) come from Debian above.
RUN --mount=type=bind,from=fetch,source=/src,target=/src \
    python3 -m zipfile -e /src/rknn_toolkit_lite2-2.3.2-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl \
      /usr/local/lib/python3.11/dist-packages/

# The blob is EGL, GLES and GBM in one library; the usual sonames point at it
# so anything linked the ordinary way finds it. No Mesa is installed to compete.
COPY --from=fetch /src/libmali-bifrost-g52-g29p1-gles.so /usr/lib/aarch64-linux-gnu/
RUN cd /usr/lib/aarch64-linux-gnu \
 && for l in libmali.so.1 libEGL.so.1 libGLESv2.so.2 libgbm.so.1; do \
      ln -s libmali-bifrost-g52-g29p1-gles.so "$l"; done

COPY --from=build /out/usr/local/lib/ /usr/local/lib/
COPY --from=build /out/usr/local/bin/mpp_info_test /out/usr/local/bin/mpi_dec_test \
     /out/usr/local/bin/mpi_enc_test /out/usr/local/bin/eglprobe /usr/local/bin/
RUN ldconfig

COPY --from=clips /clips /opt/rockchip-probe/clips
COPY --from=fetch /src/resnet18_for_rk3566_rk3568.rknn /src/space_shuttle_224.jpg /src/synset_label.py \
     /opt/rockchip-probe/npu/
COPY licenses /licenses
COPY SOURCES config.yaml /opt/rockchip-probe/
COPY probe/ /opt/rockchip-probe/

# Fail the build, not the bench run, on a missing library. The decoded test
# image must also be bit-identical to what the official example feeds the model
# (it decodes with OpenCV; this image uses Pillow).
RUN set -eu; \
    for f in /usr/local/bin/eglprobe /usr/local/bin/mpp_info_test /usr/local/bin/mpi_dec_test \
             /usr/local/bin/mpi_enc_test /usr/lib/librknnrt.so; do \
      if ldd "$f" | grep -q 'not found'; then ldd "$f"; exit 1; fi; \
    done; \
    python3 -c 'import rknnlite.api'; \
    python3 /opt/rockchip-probe/npu_worker.py --check-input /opt/rockchip-probe/npu/space_shuttle_224.jpg

EXPOSE 8000
CMD ["python3", "-u", "/opt/rockchip-probe/rockchip_probe.py"]
