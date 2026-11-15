// rgaprobe: does the RGA 2D accelerator produce correct output through librga's
// im2d API, for each kind of buffer an app can hand it?
//
//   rgaprobe [--buffers system,va,...] [--iterations N] [--stress N]
//   rgaprobe --selftest
//
// One line per result: `case=<name> buf=<buffer> ok=0|1 ms=<mean> ... detail=<text>`.
// detail is always last and runs to the end of the line. Every RGA result is
// checked against a CPU reference computed here, so a job that "succeeds" but
// writes the wrong pixels fails. Jobs are synchronous and nothing waits on
// input; a wedged driver is the caller's timeout to handle.

// librga's headers use NULL without including anything that defines it.
#include <cstddef>
#include <cstdio>

#include <im2d.hpp>
#include <rga.h>

#include <linux/dma-buf.h>
#include <linux/dma-heap.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <functional>
#include <map>
#include <set>
#include <string>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <vector>

using Bytes = std::vector<uint8_t>;

static const int PAD = 114;  // the grey YOLO-style letterboxes pad with
static const double MAE_RESIZE = 6.0, MAE_CVT = 3.0, MAE_LETTERBOX = 6.0;

static std::string one_line(const char *s) {
  std::string out = s ? s : "(null)";
  for (auto &c : out)
    if (c == '\n' || c == '\r') c = '|';
  while (!out.empty() && (out.back() == '|' || out.back() == ' ')) out.pop_back();
  return out;
}

static double now_ms() {
  using namespace std::chrono;
  return duration<double, std::milli>(steady_clock::now().time_since_epoch()).count();
}

static uint64_t fnv1a(const uint8_t *p, size_t n) {
  uint64_t h = 1469598103934665603ull;
  for (size_t i = 0; i < n; i++) h = (h ^ p[i]) * 1099511628211ull;
  return h;
}

static uint8_t clamp8(double v) { return (uint8_t)std::lround(std::min(255.0, std::max(0.0, v))); }


// Smooth gradients for the bulk (what scalers should reproduce closely), colour
// bars and a few hard lines and a checker block (where scaler kernels differ),
// so a wrong kernel, channel order or offset shows without noise drowning it.
static Bytes pattern_rgba(int w, int h) {
  static const uint8_t bars[8][3] = {{255, 255, 255}, {255, 255, 0}, {0, 255, 255}, {0, 255, 0},
                                     {255, 0, 255},   {255, 0, 0},   {0, 0, 255},   {0, 0, 0}};
  Bytes img((size_t)w * h * 4);
  for (int y = 0; y < h; y++)
    for (int x = 0; x < w; x++) {
      uint8_t *p = &img[((size_t)y * w + x) * 4];
      if (y < h / 6) {
        const uint8_t *b = bars[x * 8 / w];
        p[0] = b[0], p[1] = b[1], p[2] = b[2];
      } else {
        p[0] = (uint8_t)(255 * x / (w - 1));
        p[1] = (uint8_t)(255 * y / (h - 1));
        p[2] = (uint8_t)(255 * (x + y) / (w + h - 2));
      }
      if (std::abs(x - w / 3) < 3) p[0] = p[1] = p[2] = 255;
      if (std::abs(y - 2 * h / 3) < 3) p[0] = p[1] = p[2] = 0;
      int cx = x - 3 * w / 4, cy = y - h / 2;
      if (cx >= 0 && cx < 64 && cy >= 0 && cy < 64) p[0] = p[1] = p[2] = (((cx / 8) + (cy / 8)) & 1) ? 230 : 20;
      p[3] = 255;
    }
  return img;
}

static void rgb_to_yuv601_limited(double r, double g, double b, double &y, double &u, double &v) {
  y = 16 + 0.257 * r + 0.504 * g + 0.098 * b;
  u = 128 - 0.148 * r - 0.291 * g + 0.439 * b;
  v = 128 + 0.439 * r - 0.368 * g - 0.071 * b;
}

static Bytes rgba_to_nv12(const Bytes &rgba, int w, int h) {
  Bytes out((size_t)w * h * 3 / 2);
  uint8_t *uv = &out[(size_t)w * h];
  for (int y = 0; y < h; y++)
    for (int x = 0; x < w; x++) {
      const uint8_t *p = &rgba[((size_t)y * w + x) * 4];
      double Y, U, V;
      rgb_to_yuv601_limited(p[0], p[1], p[2], Y, U, V);
      out[(size_t)y * w + x] = clamp8(Y);
    }
  for (int y = 0; y < h; y += 2)
    for (int x = 0; x < w; x += 2) {
      double su = 0, sv = 0;
      for (int dy = 0; dy < 2; dy++)
        for (int dx = 0; dx < 2; dx++) {
          const uint8_t *p = &rgba[((size_t)(y + dy) * w + x + dx) * 4];
          double Y, U, V;
          rgb_to_yuv601_limited(p[0], p[1], p[2], Y, U, V);
          su += U, sv += V;
        }
      uv[(size_t)(y / 2) * w + x] = clamp8(su / 4);
      uv[(size_t)(y / 2) * w + x + 1] = clamp8(sv / 4);
    }
  return out;
}

static void yuv_to_rgb601(int Y, int U, int V, bool full, uint8_t *rgb) {
  double d = U - 128, e = V - 128;
  if (full) {
    rgb[0] = clamp8(Y + 1.402 * e);
    rgb[1] = clamp8(Y - 0.344136 * d - 0.714136 * e);
    rgb[2] = clamp8(Y + 1.772 * d);
  } else {
    double c = 1.164 * (Y - 16);
    rgb[0] = clamp8(c + 1.596 * e);
    rgb[1] = clamp8(c - 0.392 * d - 0.813 * e);
    rgb[2] = clamp8(c + 2.017 * d);
  }
}

// Chroma is replicated (nearest), which is what a 4:2:0 converter without a
// chroma filter does.
static Bytes nv12_to_rgb888(const Bytes &nv12, int w, int h, bool full) {
  Bytes out((size_t)w * h * 3);
  const uint8_t *uv = &nv12[(size_t)w * h];
  for (int y = 0; y < h; y++)
    for (int x = 0; x < w; x++) {
      const uint8_t *c = &uv[(size_t)(y / 2) * w + (x & ~1)];
      yuv_to_rgb601(nv12[(size_t)y * w + x], c[0], c[1], full, &out[((size_t)y * w + x) * 3]);
    }
  return out;
}

enum Kernel { BILINEAR, NEAREST, AREA };
static const char *kernel_name(Kernel k) { return k == BILINEAR ? "bilinear" : k == NEAREST ? "nearest" : "area"; }

// Interleaved planes with `ch` channels; half-pixel centres as most scalers use.
static Bytes resize_plane(const uint8_t *src, int sw, int sh, int ch, int dw, int dh, Kernel k) {
  Bytes out((size_t)dw * dh * ch);
  double fx = (double)sw / dw, fy = (double)sh / dh;
  for (int y = 0; y < dh; y++)
    for (int x = 0; x < dw; x++)
      for (int c = 0; c < ch; c++) {
        double v;
        if (k == NEAREST) {
          int sx = std::min(sw - 1, (int)((x + 0.5) * fx)), sy = std::min(sh - 1, (int)((y + 0.5) * fy));
          v = src[((size_t)sy * sw + sx) * ch + c];
        } else if (k == AREA) {
          int x0 = (int)(x * fx), x1 = std::max(x0 + 1, std::min(sw, (int)std::ceil((x + 1) * fx)));
          int y0 = (int)(y * fy), y1 = std::max(y0 + 1, std::min(sh, (int)std::ceil((y + 1) * fy)));
          double s = 0;
          for (int yy = y0; yy < y1; yy++)
            for (int xx = x0; xx < x1; xx++) s += src[((size_t)yy * sw + xx) * ch + c];
          v = s / ((x1 - x0) * (y1 - y0));
        } else {
          double sx = std::max(0.0, (x + 0.5) * fx - 0.5), sy = std::max(0.0, (y + 0.5) * fy - 0.5);
          int x0 = std::min(sw - 1, (int)sx), y0 = std::min(sh - 1, (int)sy);
          int x1 = std::min(sw - 1, x0 + 1), y1 = std::min(sh - 1, y0 + 1);
          double ax = sx - x0, ay = sy - y0;
          auto at = [&](int xx, int yy) { return (double)src[((size_t)yy * sw + xx) * ch + c]; };
          v = (at(x0, y0) * (1 - ax) + at(x1, y0) * ax) * (1 - ay) + (at(x0, y1) * (1 - ax) + at(x1, y1) * ax) * ay;
        }
        out[((size_t)y * dw + x) * ch + c] = clamp8(v);
      }
  return out;
}

static Bytes resize_nv12(const Bytes &src, int sw, int sh, int dw, int dh, Kernel k) {
  Bytes y = resize_plane(src.data(), sw, sh, 1, dw, dh, k);
  Bytes uv = resize_plane(src.data() + (size_t)sw * sh, sw / 2, sh / 2, 2, dw / 2, dh / 2, k);
  y.insert(y.end(), uv.begin(), uv.end());
  return y;
}

struct Box { int x, y, w, h; };

static Box letterbox_box(int sw, int sh, int dw, int dh) {
  double s = std::min((double)dw / sw, (double)dh / sh);
  int w = ((int)std::lround(sw * s)) & ~1, h = ((int)std::lround(sh * s)) & ~1;
  return {((dw - w) / 2) & ~1, ((dh - h) / 2) & ~1, w, h};
}

static Bytes letterbox_ref(const Bytes &nv12, int sw, int sh, int dw, int dh, bool full, Kernel k) {
  Box b = letterbox_box(sw, sh, dw, dh);
  Bytes rgb = nv12_to_rgb888(resize_nv12(nv12, sw, sh, b.w, b.h, k), b.w, b.h, full);
  Bytes out((size_t)dw * dh * 3, PAD);
  for (int y = 0; y < b.h; y++)
    memcpy(&out[((size_t)(b.y + y) * dw + b.x) * 3], &rgb[(size_t)y * b.w * 3], (size_t)b.w * 3);
  return out;
}

static Bytes crop_rgba(const Bytes &src, int sw, Box r) {
  Bytes out((size_t)r.w * r.h * 4);
  for (int y = 0; y < r.h; y++)
    memcpy(&out[(size_t)y * r.w * 4], &src[((size_t)(r.y + y) * sw + r.x) * 4], (size_t)r.w * 4);
  return out;
}

static Bytes rot90(const Bytes &src, int w, int h, bool cw) {  // output is h x w
  Bytes out(src.size());
  for (int y = 0; y < h; y++)
    for (int x = 0; x < w; x++) {
      int nx = cw ? h - 1 - y : y, ny = cw ? x : w - 1 - x;
      memcpy(&out[((size_t)ny * h + nx) * 4], &src[((size_t)y * w + x) * 4], 4);
    }
  return out;
}

static Bytes flip_h(const Bytes &src, int w, int h) {
  Bytes out(src.size());
  for (int y = 0; y < h; y++)
    for (int x = 0; x < w; x++) memcpy(&out[((size_t)y * w + w - 1 - x) * 4], &src[((size_t)y * w + x) * 4], 4);
  return out;
}

struct Err { double mae; int max; };

static Err compare(const uint8_t *a, const uint8_t *b, size_t n) {
  double sum = 0;
  int mx = 0;
  for (size_t i = 0; i < n; i++) {
    int d = std::abs((int)a[i] - (int)b[i]);
    sum += d;
    mx = std::max(mx, d);
  }
  return {n ? sum / n : 0, mx};
}

static Bytes swap_rb(const Bytes &rgb) {
  Bytes out = rgb;
  for (size_t i = 0; i + 2 < out.size(); i += 3) std::swap(out[i], out[i + 2]);
  return out;
}


struct Buf {
  std::string kind;
  bool wrap = false;  // user memory handed over per job, never imported
  uint8_t *ptr = nullptr;
  size_t size = 0;
  int fd = -1;
  rga_buffer_handle_t handle = 0;
  std::string error;

  bool cpu(bool begin) {
    if (fd < 0) return true;
    struct dma_buf_sync s = {(uint64_t)((begin ? DMA_BUF_SYNC_START : DMA_BUF_SYNC_END) | DMA_BUF_SYNC_RW)};
    return ioctl(fd, DMA_BUF_IOCTL_SYNC, &s) == 0;
  }
  void write(const Bytes &b) { cpu(true); memcpy(ptr, b.data(), b.size()); cpu(false); }
  void fill(uint8_t v) { cpu(true); memset(ptr, v, size); cpu(false); }
  Bytes read(size_t n) { cpu(true); Bytes b(ptr, ptr + n); cpu(false); return b; }
  ~Buf() {
    if (handle) releasebuffer_handle(handle);
    if (fd >= 0) {
      if (ptr) munmap(ptr, size);
      close(fd);
    } else {
      free(ptr);
    }
  }
};

static std::string heap_path(const std::string &kind) {
  if (kind == "rk-dma-heap-cma") return "/dev/rk_dma_heap/rk-dma-heap-cma";
  return "/dev/dma_heap/" + kind;
}

// Returns a buffer with `error` set instead of throwing, so one refused heap
// does not stop the others from being tried.
static Buf *alloc_buf(const std::string &kind, size_t size) {
  Buf *b = new Buf;
  b->kind = kind;
  b->size = (size + 4095) & ~(size_t)4095;
  if (kind == "va" || kind == "va-wrap") {
    void *p = nullptr;
    if (posix_memalign(&p, 4096, b->size) != 0) {
      b->error = "posix_memalign failed";
      return b;
    }
    b->ptr = (uint8_t *)p;
    if (kind == "va-wrap") {
      b->wrap = true;
      return b;
    }
    b->handle = importbuffer_virtualaddr(b->ptr, (int)b->size);
  } else {
    std::string path = heap_path(kind);
    int heap = open(path.c_str(), O_RDWR | O_CLOEXEC);
    if (heap < 0) {
      b->error = "open " + path + ": " + strerror(errno);
      return b;
    }
    struct dma_heap_allocation_data a = {};
    a.len = b->size;
    a.fd_flags = O_RDWR | O_CLOEXEC;
    int rc = ioctl(heap, DMA_HEAP_IOCTL_ALLOC, &a);
    int saved = errno;
    close(heap);
    if (rc < 0) {
      b->error = "DMA_HEAP_IOCTL_ALLOC on " + path + ": " + strerror(saved);
      return b;
    }
    b->fd = (int)a.fd;
    void *p = mmap(nullptr, b->size, PROT_READ | PROT_WRITE, MAP_SHARED, b->fd, 0);
    if (p == MAP_FAILED) {
      b->error = std::string("mmap dma-buf: ") + strerror(errno);
      return b;
    }
    b->ptr = (uint8_t *)p;
    b->handle = importbuffer_fd(b->fd, (int)b->size);
  }
  if (!b->handle) b->error = "librga refused to import the buffer (handle 0)";
  return b;
}

// va-wrap takes librga's legacy path: the address goes into each request and
// the driver maps it per job instead of at import.
static rga_buffer_t wrap(Buf *b, int w, int h, int format) {
  return b->wrap ? wrapbuffer_virtualaddr_t(b->ptr, w, h, w, h, format) : wrapbuffer_handle(b->handle, w, h, format);
}


static bool g_all_ok = true;
// Only dma-heap system buffers gate: on RK356x the vendor driver refuses
// non-contiguous user memory for RGA2 (see the README).
static std::set<std::string> g_gating_buffers = {"system"};

struct Timing { double mean = 0, min = 0, max = 0; int n = 0; };

static void emit(const std::string &name, const std::string &buf, bool ok, const Timing &t,
                 const std::string &extra, const std::string &detail) {
  printf("case=%s buf=%s ok=%d", name.c_str(), buf.c_str(), ok ? 1 : 0);
  if (t.n) printf(" ms=%.3f min=%.3f max=%.3f n=%d", t.mean, t.min, t.max, t.n);
  if (!extra.empty()) printf(" %s", extra.c_str());
  printf(" detail=%s\n", one_line(detail.c_str()).c_str());
  fflush(stdout);
  if (!ok && (buf == "-" || g_gating_buffers.count(buf))) g_all_ok = false;
}

static std::string status_text(IM_STATUS s) { return std::to_string((int)s) + " " + one_line(imStrError_t(s)); }
static bool good(IM_STATUS s) { return s == IM_STATUS_SUCCESS || s == IM_STATUS_NOERROR; }

static Timing time_job(int iterations, const std::function<IM_STATUS()> &job, std::string &error) {
  Timing t;
  t.min = 1e9;
  double sum = 0;
  for (int i = 0; i < iterations; i++) {
    double t0 = now_ms();
    IM_STATUS s = job();
    double dt = now_ms() - t0;
    if (!good(s)) {
      error = "timed run " + std::to_string(i) + ": " + status_text(s);
      break;
    }
    sum += dt, t.n++;
    t.min = std::min(t.min, dt), t.max = std::max(t.max, dt);
  }
  t.mean = t.n ? sum / t.n : 0;
  if (!t.n) t.min = 0;
  return t;
}

static std::string fmt(const char *f, double a, double b = 0) {
  char buf[128];
  snprintf(buf, sizeof buf, f, a, b);
  return buf;
}


struct Mode { const char *name; int mode; bool full; };
static const Mode MODES[] = {{"bt601-limited", IM_YUV_TO_RGB_BT601_LIMIT, false},
                             {"bt601-full", IM_YUV_TO_RGB_BT601_FULL, true}};

struct Ctx {
  std::string kind;
  int iterations;
};

// Allocates the in/out buffers of one case, or reports why it could not.
static bool buffers(const Ctx &c, const std::string &name, size_t in_size, size_t out_size, Buf *&in, Buf *&out) {
  in = alloc_buf(c.kind, in_size);
  out = alloc_buf(c.kind, out_size);
  std::string e = !in->error.empty() ? in->error : out->error;
  if (e.empty()) return true;
  emit(name, c.kind, false, {}, "", e);
  delete in, delete out;
  in = out = nullptr;
  return false;
}

static void case_copy(const Ctx &c) {
  const int w = 1280, h = 720;
  Bytes src = pattern_rgba(w, h);
  Buf *in, *out;
  if (!buffers(c, "copy", src.size(), src.size(), in, out)) return;
  in->write(src);
  out->fill(0xcd);
  rga_buffer_t s = wrap(in, w, h, RK_FORMAT_RGBA_8888);
  rga_buffer_t d = wrap(out, w, h, RK_FORMAT_RGBA_8888);
  auto job = [&] { return imcopy(s, d); };
  IM_STATUS st = job();
  if (!good(st)) {
    emit("copy", c.kind, false, {}, "", "imcopy: " + status_text(st));
  } else {
    Bytes got = out->read(src.size());
    size_t diff = 0;
    for (size_t i = 0; i < src.size(); i++) diff += got[i] != src[i];
    std::string terr;
    Timing t = time_job(c.iterations, job, terr);
    emit("copy", c.kind, diff == 0 && terr.empty(), t, "",
         (diff ? std::to_string(diff) + " bytes differ" : std::string("byte-exact")) + (terr.empty() ? "" : "; " + terr));
  }
  delete in, delete out;
}

// Picks the reference kernel that the hardware matches best; scalers differ in
// kernel, and the check is "close to a correct scaler", not "this exact one".
static void best_of(const std::function<Bytes(Kernel)> &ref, const Bytes &got, Err &best, Kernel &which) {
  best = {1e9, 255};
  for (Kernel k : {BILINEAR, AREA, NEAREST}) {
    Bytes r = ref(k);
    Err e = compare(got.data(), r.data(), r.size());
    if (e.mae < best.mae) best = e, which = k;
  }
}

static void case_resize(const Ctx &c) {
  const int sw = 1920, sh = 1080;
  Bytes rgba = pattern_rgba(sw, sh);
  {
    const int dw = 640, dh = 360;
    Bytes src = rgba_to_nv12(rgba, sw, sh);
    size_t out_size = (size_t)dw * dh * 3 / 2;
    Buf *in, *out;
    if (buffers(c, "resize-nv12", src.size(), out_size, in, out)) {
      in->write(src);
      out->fill(0xcd);
      rga_buffer_t s = wrap(in, sw, sh, RK_FORMAT_YCbCr_420_SP);
      rga_buffer_t d = wrap(out, dw, dh, RK_FORMAT_YCbCr_420_SP);
      auto job = [&] { return imresize(s, d); };
      IM_STATUS st = job();
      if (!good(st)) {
        emit("resize-nv12", c.kind, false, {}, "", "imresize: " + status_text(st));
      } else {
        Bytes got = out->read(out_size);
        Err e;
        Kernel k = BILINEAR;
        best_of([&](Kernel kk) { return resize_nv12(src, sw, sh, dw, dh, kk); }, got, e, k);
        std::string terr;
        Timing t = time_job(c.iterations, job, terr);
        emit("resize-nv12", c.kind, e.mae <= MAE_RESIZE && terr.empty(), t,
             std::string("mae=") + fmt("%.3f", e.mae) + " maxerr=" + std::to_string(e.max),
             std::string("1920x1080 -> 640x360 NV12, closest CPU reference ") + kernel_name(k) +
                 fmt(", threshold mae<=%.1f", MAE_RESIZE) + (terr.empty() ? "" : "; " + terr));
      }
      delete in, delete out;
    }
  }
  {
    const int dw = 640, dh = 640;
    size_t out_size = (size_t)dw * dh * 4;
    Buf *in, *out;
    if (buffers(c, "resize-rgba", rgba.size(), out_size, in, out)) {
      in->write(rgba);
      out->fill(0xcd);
      rga_buffer_t s = wrap(in, sw, sh, RK_FORMAT_RGBA_8888);
      rga_buffer_t d = wrap(out, dw, dh, RK_FORMAT_RGBA_8888);
      auto job = [&] { return imresize(s, d); };
      IM_STATUS st = job();
      if (!good(st)) {
        emit("resize-rgba", c.kind, false, {}, "", "imresize: " + status_text(st));
      } else {
        Bytes got = out->read(out_size);
        Err e;
        Kernel k = BILINEAR;
        best_of([&](Kernel kk) { return resize_plane(rgba.data(), sw, sh, 4, dw, dh, kk); }, got, e, k);
        std::string terr;
        Timing t = time_job(c.iterations, job, terr);
        emit("resize-rgba", c.kind, e.mae <= MAE_RESIZE && terr.empty(), t,
             std::string("mae=") + fmt("%.3f", e.mae) + " maxerr=" + std::to_string(e.max),
             std::string("1920x1080 -> 640x640 RGBA (non-uniform), closest CPU reference ") + kernel_name(k) +
                 fmt(", threshold mae<=%.1f", MAE_RESIZE) + (terr.empty() ? "" : "; " + terr));
      }
      delete in, delete out;
    }
  }
}

// The mode is always requested explicitly, and the output must match the
// requested range and be closer to it than to the other: imcvtcolor and
// improcess pick different defaults otherwise.
static void case_cvtcolor(const Ctx &c, const Mode &m) {
  const int w = 1280, h = 720;
  const std::string name = std::string("cvtcolor-") + m.name;
  Bytes src = rgba_to_nv12(pattern_rgba(w, h), w, h);
  size_t out_size = (size_t)w * h * 3;
  Buf *in, *out;
  if (!buffers(c, name, src.size(), out_size, in, out)) return;
  in->write(src);
  out->fill(0xcd);
  rga_buffer_t s = wrap(in, w, h, RK_FORMAT_YCbCr_420_SP);
  rga_buffer_t d = wrap(out, w, h, RK_FORMAT_RGB_888);
  auto job = [&] { return imcvtcolor(s, d, RK_FORMAT_YCbCr_420_SP, RK_FORMAT_RGB_888, m.mode); };
  IM_STATUS st = job();
  if (!good(st)) {
    emit(name, c.kind, false, {}, std::string("requested=") + m.name, "imcvtcolor: " + status_text(st));
  } else {
    Bytes got = out->read(out_size);
    Bytes ref = nv12_to_rgb888(src, w, h, m.full), other = nv12_to_rgb888(src, w, h, !m.full);
    Err e = compare(got.data(), ref.data(), ref.size());
    Err o = compare(got.data(), other.data(), other.size());
    Err swapped = compare(got.data(), swap_rb(ref).data(), ref.size());
    bool ok = e.mae <= MAE_CVT && e.mae < o.mae;
    std::string terr;
    Timing t = time_job(c.iterations, job, terr);
    emit(name, c.kind, ok && terr.empty(), t,
         std::string("requested=") + m.name + " mae=" + fmt("%.3f", e.mae) + " maxerr=" + std::to_string(e.max) +
             " other_range_mae=" + fmt("%.3f", o.mae),
         std::string("NV12 1280x720 -> RGB888, output vs CPU BT.601 ") + m.name +
             (e.mae < o.mae ? " (matches the requested range)" : " (closer to the other range)") +
             (swapped.mae < e.mae ? fmt("; R/B swapped would give mae %.3f", swapped.mae) : "") +
             fmt("; threshold mae<=%.1f", MAE_CVT) + (terr.empty() ? "" : "; " + terr));
  }
  delete in, delete out;
}

// The NVR's step: camera NV12 to a padded RGB888 square for the detector, in
// sync jobs (pad bands by fill, then one scale+convert into the centre).
static IM_STATUS letterbox_job(rga_buffer_t s, rga_buffer_t d, int sw, int sh, int dw, int dh, int mode) {
  Box b = letterbox_box(sw, sh, dw, dh);
  // librga and the RGA2 driver pass the fill colour to the hardware register
  // untouched, and which byte lands in which channel depends on the output
  // format (RGBA puts bits 7:0 in R). The same value in all four bytes is grey
  // whatever the mapping.
  const int grey = PAD * 0x01010101;
  IM_STATUS st;
  if (b.y > 0) {
    if (!good(st = imfill(d, im_rect{0, 0, dw, b.y}, grey))) return st;
    if (!good(st = imfill(d, im_rect{0, b.y + b.h, dw, dh - b.y - b.h}, grey))) return st;
  }
  if (b.x > 0) {
    if (!good(st = imfill(d, im_rect{0, 0, b.x, dh}, grey))) return st;
    if (!good(st = imfill(d, im_rect{b.x + b.w, 0, dw - b.x - b.w, dh}, grey))) return st;
  }
  rga_buffer_t pat;
  memset(&pat, 0, sizeof pat);
  d.color_space_mode = mode;
  return improcess(s, d, pat, im_rect{0, 0, sw, sh}, im_rect{b.x, b.y, b.w, b.h}, im_rect{0, 0, 0, 0}, IM_SYNC);
}

struct LetterboxResult {
  bool ok;
  size_t pad_bad;
  Err content, other;
  std::string kernel, detail;
};

static Err compare_box(const Bytes &a, const Bytes &b, int dw, Box box) {
  double sum = 0;
  int mx = 0;
  for (int y = box.y; y < box.y + box.h; y++) {
    Err e = compare(&a[((size_t)y * dw + box.x) * 3], &b[((size_t)y * dw + box.x) * 3], (size_t)box.w * 3);
    sum += e.mae * box.w * 3, mx = std::max(mx, e.max);
  }
  return {sum / ((double)box.w * box.h * 3), mx};
}

static LetterboxResult check_letterbox(const Bytes &src, const Bytes &got, int sw, int sh, int dw, int dh, bool full) {
  Box b = letterbox_box(sw, sh, dw, dh);
  LetterboxResult r{false, 0, {1e9, 255}, {1e9, 255}, "none", ""};
  for (int y = 0; y < dh; y++)
    for (int x = 0; x < dw; x++)
      if (y < b.y || y >= b.y + b.h || x < b.x || x >= b.x + b.w)
        for (int ch = 0; ch < 3; ch++) r.pad_bad += got[((size_t)y * dw + x) * 3 + ch] != PAD;
  for (bool range : {full, !full})
    for (Kernel k : {BILINEAR, AREA, NEAREST}) {
      Err e = compare_box(got, letterbox_ref(src, sw, sh, dw, dh, range, k), dw, b);
      Err &slot = range == full ? r.content : r.other;
      if (e.mae < slot.mae) {
        slot = e;
        if (range == full) r.kernel = kernel_name(k);
      }
    }
  r.ok = r.pad_bad == 0 && r.content.mae <= MAE_LETTERBOX && r.content.mae < r.other.mae;
  r.detail = "NV12 " + std::to_string(sw) + "x" + std::to_string(sh) + " -> RGB888 " + std::to_string(dw) + "x" +
             std::to_string(dh) + ", content " + std::to_string(b.w) + "x" + std::to_string(b.h) + " at y=" +
             std::to_string(b.y) + " vs " + (full ? "bt601-full" : "bt601-limited") + "/" + r.kernel +
             (r.content.mae < r.other.mae ? " (matches the requested range)" : " (closer to the other range)") + ", pad " +
             std::to_string(PAD) + ": " + std::to_string(r.pad_bad) + " pad bytes wrong" +
             fmt("; threshold content mae<=%.1f, pad exact", MAE_LETTERBOX);
  return r;
}

static void case_letterbox(const Ctx &c, const Mode &m) {
  const int sw = 1920, sh = 1080, dw = 640, dh = 640;
  const std::string name = std::string("letterbox-") + m.name;
  Bytes src = rgba_to_nv12(pattern_rgba(sw, sh), sw, sh);
  size_t out_size = (size_t)dw * dh * 3;
  Buf *in, *out;
  if (!buffers(c, name, src.size(), out_size, in, out)) return;
  in->write(src);
  out->fill(0xcd);
  rga_buffer_t s = wrap(in, sw, sh, RK_FORMAT_YCbCr_420_SP);
  rga_buffer_t d = wrap(out, dw, dh, RK_FORMAT_RGB_888);
  auto job = [&] { return letterbox_job(s, d, sw, sh, dw, dh, m.mode); };
  IM_STATUS st = job();
  if (!good(st)) {
    emit(name, c.kind, false, {}, std::string("requested=") + m.name, "letterbox: " + status_text(st));
  } else {
    LetterboxResult r = check_letterbox(src, out->read(out_size), sw, sh, dw, dh, m.full);
    std::string terr;
    Timing t = time_job(c.iterations, job, terr);
    emit(name, c.kind, r.ok && terr.empty(), t,
         std::string("requested=") + m.name + " content_mae=" + fmt("%.3f", r.content.mae) +
             " content_maxerr=" + std::to_string(r.content.max) + " other_range_mae=" + fmt("%.3f", r.other.mae) +
             " pad_bad=" + std::to_string(r.pad_bad) + " kernel=" + r.kernel,
         r.detail + (terr.empty() ? "" : "; " + terr));
  }
  delete in, delete out;
}

static void case_crop_rotate_flip(const Ctx &c) {
  const int w = 1280, h = 720;
  const Box crop = {64, 32, 320, 240};
  Bytes src = pattern_rgba(w, h);
  size_t out_size = (size_t)crop.w * crop.h * 4;
  Buf *in, *out;
  if (!buffers(c, "crop-rotate-flip", src.size(), out_size, in, out)) return;
  in->write(src);
  out->fill(0xcd);
  rga_buffer_t s = wrap(in, w, h, RK_FORMAT_RGBA_8888);
  rga_buffer_t d = wrap(out, crop.h, crop.w, RK_FORMAT_RGBA_8888);
  rga_buffer_t pat;
  memset(&pat, 0, sizeof pat);
  auto job = [&] {
    return improcess(s, d, pat, im_rect{crop.x, crop.y, crop.w, crop.h}, im_rect{0, 0, crop.h, crop.w},
                     im_rect{0, 0, 0, 0}, IM_SYNC | IM_HAL_TRANSFORM_ROT_90 | IM_HAL_TRANSFORM_FLIP_H);
  };
  IM_STATUS st = job();
  if (!good(st)) {
    emit("crop-rotate-flip", c.kind, false, {}, "", "improcess: " + status_text(st));
  } else {
    Bytes got = out->read(out_size);
    Bytes cut = crop_rgba(src, w, crop);
    // The order of rotate and flip inside one job, and the rotation direction,
    // are not documented precisely; any exact geometric match proves the job.
    std::map<std::string, Bytes> refs = {
        {"rot90cw-then-fliph", flip_h(rot90(cut, crop.w, crop.h, true), crop.h, crop.w)},
        {"fliph-then-rot90cw", rot90(flip_h(cut, crop.w, crop.h), crop.w, crop.h, true)},
        {"rot90ccw-then-fliph", flip_h(rot90(cut, crop.w, crop.h, false), crop.h, crop.w)},
        {"fliph-then-rot90ccw", rot90(flip_h(cut, crop.w, crop.h), crop.w, crop.h, false)},
    };
    std::string matched;
    for (auto &kv : refs)
      if (kv.second == got) matched += (matched.empty() ? "" : ",") + kv.first;
    std::string terr;
    Timing t = time_job(c.iterations, job, terr);
    emit("crop-rotate-flip", c.kind, !matched.empty() && terr.empty(), t, "matched=" + (matched.empty() ? "none" : matched),
         "crop 320x240 at (64,32) of RGBA 1280x720, ROT_90|FLIP_H into 240x320, byte-exact against each order" +
             (terr.empty() ? "" : "; " + terr));
  }
  delete in, delete out;
}

static void case_fill(const Ctx &c) {
  const int w = 1280, h = 720;
  const Box r = {100, 50, 320, 200};
  const int color = 0x11223344;
  Bytes src = pattern_rgba(w, h);
  Buf *in, *out;
  if (!buffers(c, "fill", 4096, src.size(), in, out)) return;
  out->write(src);
  rga_buffer_t d = wrap(out, w, h, RK_FORMAT_RGBA_8888);
  auto job = [&] { return imfill(d, im_rect{r.x, r.y, r.w, r.h}, color); };
  IM_STATUS st = job();
  if (!good(st)) {
    emit("fill", c.kind, false, {}, "", "imfill: " + status_text(st));
  } else {
    Bytes got = out->read(src.size());
    // The guide gives the colour as R,G,B,A from the high byte down; accept
    // either memory order but require the whole rect uniform and nothing else touched.
    const uint8_t doc[4] = {0x11, 0x22, 0x33, 0x44}, rev[4] = {0x44, 0x33, 0x22, 0x11};
    size_t outside = 0, doc_bad = 0, rev_bad = 0;
    for (int y = 0; y < h; y++)
      for (int x = 0; x < w; x++) {
        size_t i = ((size_t)y * w + x) * 4;
        bool in_rect = x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h;
        if (!in_rect) {
          outside += memcmp(&got[i], &src[i], 4) != 0;
        } else {
          doc_bad += memcmp(&got[i], doc, 4) != 0;
          rev_bad += memcmp(&got[i], rev, 4) != 0;
        }
      }
    const char *order = doc_bad == 0 ? "R,G,B,A in memory (as documented)" : rev_bad == 0 ? "A,B,G,R in memory" : "neither";
    std::string terr;
    Timing t = time_job(c.iterations, job, terr);
    emit("fill", c.kind, outside == 0 && (doc_bad == 0 || rev_bad == 0) && terr.empty(), t, "",
         std::string("rect 320x200 at (100,50) colour 0x11223344: byte order ") + order + "; " +
             std::to_string(std::min(doc_bad, rev_bad)) + " wrong pixels inside, " + std::to_string(outside) +
             " changed outside" + (terr.empty() ? "" : "; " + terr));
  }
  delete in, delete out;
}

// Records how the 32-bit fill colour maps onto a 24-bit format, which the
// guide does not state; any consistent 3-byte window of the colour passes.
static void case_fill_rgb888(const Ctx &c) {
  const int w = 1280, h = 720;
  const Box r = {100, 50, 320, 200};
  const int color = 0x44332211;
  Bytes src = nv12_to_rgb888(rgba_to_nv12(pattern_rgba(w, h), w, h), w, h, false);
  Buf *in, *out;
  if (!buffers(c, "fill-rgb888", 4096, src.size(), in, out)) return;
  out->write(src);
  rga_buffer_t d = wrap(out, w, h, RK_FORMAT_RGB_888);
  auto job = [&] { return imfill(d, im_rect{r.x, r.y, r.w, r.h}, color); };
  IM_STATUS st = job();
  if (!good(st)) {
    emit("fill-rgb888", c.kind, false, {}, "", "imfill: " + status_text(st));
  } else {
    Bytes got = out->read(src.size());
    const uint8_t *first = &got[((size_t)r.y * w + r.x) * 3];
    size_t outside = 0, nonuniform = 0;
    for (int y = 0; y < h; y++)
      for (int x = 0; x < w; x++) {
        size_t i = ((size_t)y * w + x) * 3;
        bool in_rect = x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h;
        if (in_rect) nonuniform += memcmp(&got[i], first, 3) != 0;
        else outside += memcmp(&got[i], &src[i], 3) != 0;
      }
    const uint8_t windows[4][3] = {{0x11, 0x22, 0x33}, {0x22, 0x33, 0x44}, {0x33, 0x22, 0x11}, {0x44, 0x33, 0x22}};
    bool known = false;
    for (auto &wnd : windows) known |= memcmp(first, wnd, 3) == 0;
    char bytes[16];
    snprintf(bytes, sizeof bytes, "%02x,%02x,%02x", first[0], first[1], first[2]);
    std::string terr;
    Timing t = time_job(c.iterations, job, terr);
    emit("fill-rgb888", c.kind, known && nonuniform == 0 && outside == 0 && terr.empty(), t,
         std::string("memory=") + bytes,
         std::string("RGB888 rect 320x200 at (100,50), colour 0x44332211 -> bytes ") + bytes + " in memory; " +
             std::to_string(nonuniform) + " non-uniform pixels inside, " + std::to_string(outside) + " changed outside" +
             (terr.empty() ? "" : "; " + terr));
  }
  delete in, delete out;
}

static void run_cpu_letterbox() {
  const int sw = 1920, sh = 1080, dw = 640, dh = 640;
  Bytes src = rgba_to_nv12(pattern_rgba(sw, sh), sw, sh);
  Timing t;
  t.min = 1e9;
  double sum = 0;
  for (int i = 0; i < 5; i++) {
    double t0 = now_ms();
    Bytes out = letterbox_ref(src, sw, sh, dw, dh, false, BILINEAR);
    double dt = now_ms() - t0;
    sum += dt, t.n++, t.min = std::min(t.min, dt), t.max = std::max(t.max, dt);
  }
  t.mean = sum / t.n;
  emit("cpu-letterbox", "cpu", true, t, "", "the same letterbox in this tool's unoptimised single-threaded C++ reference, for scale only");
}

static void run_stress(int jobs) {
  const int sw = 1920, sh = 1080, dw = 640, dh = 640;
  Bytes src = rgba_to_nv12(pattern_rgba(sw, sh), sw, sh);
  size_t out_size = (size_t)dw * dh * 3;
  Ctx c{"system", 0};
  Buf *in, *out;
  if (!buffers(c, "stress", src.size(), out_size, in, out)) return;
  in->write(src);
  rga_buffer_t s = wrap(in, sw, sh, RK_FORMAT_YCbCr_420_SP);
  rga_buffer_t d = wrap(out, dw, dh, RK_FORMAT_RGB_888);
  std::set<uint64_t> sums;
  int failures = 0;
  std::string first_error;
  Timing t;
  t.min = 1e9;
  double total = 0;
  for (int i = 0; i < jobs; i++) {
    out->fill(0);  // a job that silently does nothing must change the checksum
    double t0 = now_ms();
    IM_STATUS st = letterbox_job(s, d, sw, sh, dw, dh, IM_YUV_TO_RGB_BT601_LIMIT);
    double dt = now_ms() - t0;
    if (!good(st)) {
      if (first_error.empty()) first_error = "job " + std::to_string(i) + ": " + status_text(st);
      failures++;
      continue;
    }
    total += dt, t.n++, t.min = std::min(t.min, dt), t.max = std::max(t.max, dt);
    Bytes got = out->read(out_size);
    sums.insert(fnv1a(got.data(), got.size()));
  }
  t.mean = t.n ? total / t.n : 0;
  if (!t.n) t.min = 0;
  emit("stress", "system", failures == 0 && sums.size() == 1, t,
       "jobs=" + std::to_string(jobs) + " failures=" + std::to_string(failures) + " checksums=" + std::to_string(sums.size()),
       std::to_string(jobs) + " consecutive bt601-limited letterbox jobs on dma-heap system buffers" +
           (first_error.empty() ? "" : "; first failure " + first_error));
  delete in, delete out;
}

static void run_version() {
  const char *ver = querystring(RGA_VERSION);
  std::string v = one_line(ver);
  bool ok = ver && !v.empty() && v.find("rror") == std::string::npos && v.find("ail") == std::string::npos;
  emit("version", "-", ok, {}, "", v);
  emit("capabilities", "-", true, {}, "", one_line(querystring(RGA_ALL)));
}


static void selftest() {
  auto check = [](const char *name, bool ok, const std::string &detail) {
    emit(std::string("selftest-") + name, "-", ok, {}, "", detail);
  };
  const int w = 64, h = 48;
  Bytes a = pattern_rgba(w, h), b = pattern_rgba(w, h);
  check("pattern-deterministic", a == b, "two generations are identical");
  std::set<uint32_t> colours;
  for (size_t i = 0; i < a.size(); i += 4) colours.insert(a[i] | a[i + 1] << 8 | a[i + 2] << 16);
  check("pattern-structured", colours.size() > 500, std::to_string(colours.size()) + " distinct colours");

  // Flat colour survives the full YUV round trip within rounding.
  Bytes flat((size_t)w * h * 4);
  for (size_t i = 0; i < flat.size(); i += 4) flat[i] = 180, flat[i + 1] = 90, flat[i + 2] = 40, flat[i + 3] = 255;
  Bytes rgb = nv12_to_rgb888(rgba_to_nv12(flat, w, h), w, h, false);
  int worst = 0;
  for (size_t i = 0; i < rgb.size(); i++) worst = std::max(worst, std::abs(rgb[i] - flat[i / 3 * 4 + i % 3]));
  check("yuv-roundtrip-limited", worst <= 2, "max error " + std::to_string(worst));
  uint8_t px[3];
  yuv_to_rgb601(16, 128, 128, false, px);
  bool black = px[0] == 0 && px[1] == 0 && px[2] == 0;
  yuv_to_rgb601(235, 128, 128, false, px);
  check("yuv-limited-range", black && px[0] == 255 && px[1] == 255 && px[2] == 255, "Y16 -> black, Y235 -> white");
  yuv_to_rgb601(255, 128, 128, true, px);
  check("yuv-full-range", px[0] == 255 && px[1] == 255 && px[2] == 255, "full-range Y255 -> white");

  Bytes ident = resize_plane(a.data(), w, h, 4, w, h, BILINEAR);
  check("resize-identity", ident == a, "same-size bilinear is a copy");
  Bytes grad((size_t)256 * 4);
  for (int i = 0; i < 256; i++) grad[i] = (uint8_t)i;
  Bytes half = resize_plane(grad.data(), 256, 1, 1, 128, 1, BILINEAR);
  int gerr = 0;
  for (int i = 0; i < 128; i++) gerr = std::max(gerr, std::abs(half[i] - (2 * i + 1)));
  check("resize-gradient", gerr <= 1, "halved ramp max error " + std::to_string(gerr));
  Bytes area = resize_plane(grad.data(), 256, 1, 1, 64, 1, AREA);
  check("resize-area", area[0] == 2 && area[63] == 254, "4:1 box average of a ramp");
  for (Kernel k : {BILINEAR, AREA, NEAREST}) {
    Bytes s = resize_plane(flat.data(), w, h, 4, 20, 30, k);
    bool same = true;
    for (size_t i = 0; i < s.size(); i++) same &= s[i] == flat[i % 4];
    check((std::string("resize-flat-") + kernel_name(k)).c_str(), same, "a flat image stays flat");
  }

  Bytes r4 = a;
  int rw = w, rh = h;
  for (int i = 0; i < 4; i++) r4 = rot90(r4, rw, rh, true), std::swap(rw, rh);
  check("rotate-4x-identity", r4 == a, "four clockwise quarter turns");
  Bytes cw = rot90(a, w, h, true);
  check("rotate-direction", memcmp(&cw[((size_t)0 * h + (h - 1)) * 4], &a[0], 4) == 0, "cw puts top-left at top-right");
  check("flip-twice-identity", flip_h(flip_h(a, w, h), w, h) == a, "two horizontal flips");
  Bytes c = crop_rgba(a, w, {4, 2, 8, 6});
  check("crop", memcmp(&c[0], &a[((size_t)2 * w + 4) * 4], 4) == 0 && c.size() == 8 * 6 * 4, "8x6 at (4,2)");

  Box lb = letterbox_box(1920, 1080, 640, 640);
  check("letterbox-geometry", lb.x == 0 && lb.y == 140 && lb.w == 640 && lb.h == 360,
        "1920x1080 into 640x640 -> 640x360 at y=140");
  Bytes nv = rgba_to_nv12(pattern_rgba(192, 108), 192, 108);
  Bytes lr = letterbox_ref(nv, 192, 108, 64, 64, false, BILINEAR);
  Box sb = letterbox_box(192, 108, 64, 64);
  LetterboxResult self = check_letterbox(nv, lr, 192, 108, 64, 64, false);
  check("letterbox-reference", self.ok && self.content.mae == 0 && sb.y > 0, "the checker accepts its own reference: " + self.detail);
  Bytes broken = lr;
  broken[0] = 0;
  check("letterbox-checker-rejects-pad", !check_letterbox(nv, broken, 192, 108, 64, 64, false).ok, "a wrong pad byte fails");
  Bytes pad255 = lr;
  for (size_t i = 0; i < pad255.size(); i += 3)
    if (pad255[i] == PAD && pad255[i + 1] == PAD && pad255[i + 2] == PAD) pad255[i] = 255;
  LetterboxResult p255 = check_letterbox(nv, pad255, 192, 108, 64, 64, false);
  check("letterbox-checker-rejects-pad-channel", !p255.ok && p255.pad_bad > 0 && p255.content.mae == 0,
        "one pad channel at 255 fails on the pad while the content still matches");
  Bytes shifted = letterbox_ref(nv, 192, 108, 64, 64, false, BILINEAR);
  std::rotate(shifted.begin(), shifted.begin() + 64 * 3 * 20, shifted.end());
  check("letterbox-checker-rejects-shift", !check_letterbox(nv, shifted, 192, 108, 64, 64, false).ok, "content moved 20 rows fails");
  Bytes fullref = letterbox_ref(nv, 192, 108, 64, 64, true, BILINEAR);
  check("letterbox-checker-rejects-wrong-range", !check_letterbox(nv, fullref, 192, 108, 64, 64, false).ok &&
        check_letterbox(nv, fullref, 192, 108, 64, 64, true).ok, "full-range output fails when limited was requested");
  Bytes lim = nv12_to_rgb888(nv, 192, 108, false), ful = nv12_to_rgb888(nv, 192, 108, true);
  double range_gap = compare(lim.data(), ful.data(), lim.size()).mae;
  check("range-separation", range_gap > 2 * MAE_CVT, fmt("limited and full references differ by mae %.2f", range_gap));
  const uint32_t grey = PAD * 0x01010101u;
  check("pad-colour-packing", (grey & 0xff) == PAD && (grey >> 8 & 0xff) == PAD && (grey >> 16 & 0xff) == PAD && (grey >> 24) == PAD,
        "the pad colour is 114 in every byte");
  Bytes ref601 = nv12_to_rgb888(nv, 192, 108, false);
  check("swap-detect", compare(ref601.data(), swap_rb(ref601).data(), ref601.size()).mae > 10,
        "an R/B swap is far outside the cvtcolor threshold");
}

int main(int argc, char **argv) {
  setvbuf(stdout, nullptr, _IOLBF, 0);
  std::vector<std::string> kinds = {"system", "system-uncached", "reserved", "rk-dma-heap-cma", "va", "va-wrap"};
  int iterations = 50, stress = 500;
  for (int i = 1; i < argc; i++) {
    std::string a = argv[i];
    if (a == "--selftest") {
      selftest();
      return g_all_ok ? 0 : 1;
    } else if (a == "--buffers" && i + 1 < argc) {
      kinds.clear();
      std::string list = argv[++i];
      for (size_t p = 0, q; p <= list.size(); p = q + 1) {
        q = list.find(',', p);
        if (q == std::string::npos) q = list.size();
        if (q > p) kinds.push_back(list.substr(p, q - p));
      }
    } else if (a == "--iterations" && i + 1 < argc) {
      iterations = atoi(argv[++i]);
    } else if (a == "--stress" && i + 1 < argc) {
      stress = atoi(argv[++i]);
    } else {
      fprintf(stderr, "usage: rgaprobe [--selftest] [--buffers va,system,...] [--iterations N] [--stress N]\n");
      return 2;
    }
  }

  // librga 1.10.5 dereferences a null context (SIGSEGV) when /dev/rga cannot be
  // opened, so check first and fail every case cleanly instead.
  int rga = open("/dev/rga", O_RDWR | O_CLOEXEC);
  if (rga < 0) {
    std::string why = std::string("open /dev/rga: ") + strerror(errno) + "; librga not called";
    emit("version", "-", false, {}, "", why);
    for (const auto &k : kinds)
      for (const char *name : {"copy", "resize-nv12", "resize-rgba", "cvtcolor-bt601-limited", "cvtcolor-bt601-full",
                               "letterbox-bt601-limited", "letterbox-bt601-full", "crop-rotate-flip", "fill",
                               "fill-rgb888"})
        emit(name, k, false, {}, "", why);
    run_cpu_letterbox();
    if (stress > 0) emit("stress", "system", false, {}, "", why);
    return 1;
  }
  close(rga);

  run_version();
  for (const auto &k : kinds) {
    Ctx c{k, iterations};
    case_copy(c);
    case_resize(c);
    for (const Mode &m : MODES) case_cvtcolor(c, m);
    for (const Mode &m : MODES) case_letterbox(c, m);
    case_crop_rotate_flip(c);
    case_fill(c);
    case_fill_rgb888(c);
  }
  run_cpu_letterbox();
  if (stress > 0) run_stress(stress);
  return g_all_ok ? 0 : 1;
}
