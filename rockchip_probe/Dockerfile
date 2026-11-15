# Debian rather than Alpine: libmali and the RKNN runtime fetched at first start
# are glibc-only, and bookworm's Python 3.11 is one the rknn-toolkit-lite2 2.3.2
# wheels are built for.
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
    cmp libmali-END_USER_LICENCE_AGREEMENT.txt licenses-repo/libmali-END_USER_LICENCE_AGREEMENT.txt; \
    cmp mpp/LICENSES/Apache-2.0 licenses-repo/mpp-Apache-2.0.txt; \
    cmp mpp/LICENSES/MIT licenses-repo/mpp-MIT.txt; \
    mkdir librga && tar -xzf librga.tar.gz -C librga --strip-components=1; \
    cmp librga/COPYING licenses-repo/librga-COPYING.txt; \
    for f in drm_fourcc.h drm_mode.h; do \
      echo "core/3rdparty/libdrm/include/drm/$f:"; \
      awk '/\*\//{print; exit} {print}' "librga/core/3rdparty/libdrm/include/drm/$f"; \
      echo; \
    done > librga-drm-notice.txt; \
    cmp librga-drm-notice.txt licenses-repo/librga-libdrm-headers-NOTICE.txt

FROM ${DEBIAN} AS build
# Debian's Khronos/GBM headers are only for compiling eglprobe; the binary is
# linked against the libmali blob, which exports EGL, GLES and GBM itself.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake meson ninja-build pkg-config libdrm-dev libegl-dev libgles-dev libgbm-dev \
 && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /src/mpp /build/mpp
RUN cmake -S /build/mpp -B /build/mpp-out -DCMAKE_BUILD_TYPE=Release -DBUILD_TEST=ON \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
 && cmake --build /build/mpp-out -j"$(nproc)" \
 && DESTDIR=/out cmake --install /build/mpp-out
# librga from source rather than airockchip/librga's prebuilt .so, so the binary
# is exactly the Apache-2.0 source it claims to be.
COPY --from=fetch /src/librga /build/librga
RUN meson setup /build/rga-out /build/librga --buildtype=release --prefix=/usr/local --libdir=lib \
 && ninja -C /build/rga-out \
 && DESTDIR=/out meson install -C /build/rga-out
COPY rgaprobe.cpp /build/
RUN g++ -std=c++17 -O2 -Wall -Wextra -o /out/usr/local/bin/rgaprobe /build/rgaprobe.cpp \
      -I/out/usr/local/include/rga -L/out/usr/local/lib -lrga \
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
      ca-certificates python3 python3-numpy python3-psutil python3-ruamel.yaml python3-pil libdrm2 \
 && rm -rf /var/lib/apt/lists/*

# The RKNN runtime, lite wheel and example are not in the image: the app
# fetches them at first start (RUNTIME_SOURCES). rknnlite loads the runtime from
# this exact path, which resolves once the app points the directory link at its
# verified download. The wheel's declared deps (numpy, psutil, ruamel.yaml)
# come from Debian above.
RUN mkdir -p /var/lib/rockchip-probe \
 && ln -s /var/lib/rockchip-probe/rknn/librknnrt.so /usr/lib/librknnrt.so

# The blob is EGL, GLES and GBM in one library; the usual sonames point at it
# so anything linked the ordinary way finds it. No Mesa is installed to compete.
COPY --from=fetch /src/libmali-bifrost-g52-g29p1-gles.so /usr/lib/aarch64-linux-gnu/
RUN cd /usr/lib/aarch64-linux-gnu \
 && for l in libmali.so.1 libEGL.so.1 libGLESv2.so.2 libgbm.so.1; do \
      ln -s libmali-bifrost-g52-g29p1-gles.so "$l"; done

COPY --from=build /out/usr/local/lib/ /usr/local/lib/
COPY --from=build /out/usr/local/bin/mpp_info_test /out/usr/local/bin/mpi_dec_test \
     /out/usr/local/bin/mpi_enc_test /out/usr/local/bin/eglprobe /out/usr/local/bin/rgaprobe /usr/local/bin/
RUN ldconfig

COPY --from=clips /clips /opt/rockchip-probe/clips
COPY licenses /licenses
COPY SOURCES RUNTIME_SOURCES config.yaml /opt/rockchip-probe/
COPY probe/ /opt/rockchip-probe/

# Fail the build, not the bench run, on a missing library. The RKNN checks
# (import, runtime load, decoded test image) run after the first-start fetch
# and are recorded in the report.
RUN set -eu; \
    for f in /usr/local/bin/eglprobe /usr/local/bin/mpp_info_test /usr/local/bin/mpi_dec_test \
             /usr/local/bin/mpi_enc_test /usr/local/bin/rgaprobe; do \
      if ldd "$f" | grep -q 'not found'; then ldd "$f"; exit 1; fi; \
    done; \
    rgaprobe --selftest; \
    python3 -c 'import numpy, psutil, ruamel.yaml, PIL'; \
    python3 -c 'import sys; sys.path.insert(0, "/opt/rockchip-probe"); import rknn_fetch' 

EXPOSE 8000
CMD ["python3", "-u", "/opt/rockchip-probe/rockchip_probe.py"]
