// eglprobe: can a GLES 3 context render into an FBO on a given EGL platform?
//
//   eglprobe drm <node>          print the DRM driver name of <node>
//   eglprobe gbm <node>          EGL_PLATFORM_GBM_KHR on a gbm device over <node>
//   eglprobe surfaceless         EGL_PLATFORM_SURFACELESS_MESA
//   eglprobe default             eglGetDisplay(EGL_DEFAULT_DISPLAY)
//
// Output is key=value lines so the caller can parse it. Exit 0 only when the
// clear colour read back from the FBO is opaque red. One platform per process,
// so a blob that crashes on one path cannot hide the result of another.

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl3.h>
#include <gbm.h>
#include <xf86drm.h>

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#ifndef EGL_PLATFORM_SURFACELESS_MESA
#define EGL_PLATFORM_SURFACELESS_MESA 0x31DD
#endif
#ifndef EGL_PLATFORM_GBM_KHR
#define EGL_PLATFORM_GBM_KHR 0x31D7
#endif

static const char *str_or_null(const char *s) { return s ? s : "(null)"; }

static int render(EGLDisplay d) {
  EGLint maj, min, n = 0;
  EGLConfig cfg;

  if (d == EGL_NO_DISPLAY) {
    printf("error=no_display egl_error=0x%x\n", eglGetError());
    return 1;
  }
  if (!eglInitialize(d, &maj, &min)) {
    printf("error=initialize egl_error=0x%x\n", eglGetError());
    return 1;
  }
  printf("egl_version=%d.%d\n", maj, min);
  printf("egl_vendor=%s\n", str_or_null(eglQueryString(d, EGL_VENDOR)));
  printf("display_extensions=%s\n", str_or_null(eglQueryString(d, EGL_EXTENSIONS)));

  if (!eglBindAPI(EGL_OPENGL_ES_API)) {
    printf("error=bind_api egl_error=0x%x\n", eglGetError());
    return 1;
  }
  // SURFACE_TYPE 0: the consumer renders only into FBOs and never asks for a
  // window or pbuffer surface, so the probe must not either.
  EGLint ca[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_SURFACE_TYPE, 0, EGL_NONE};
  if (!eglChooseConfig(d, ca, &cfg, 1, &n) || n < 1) {
    printf("error=no_config egl_error=0x%x\n", eglGetError());
    return 1;
  }
  EGLint xa[] = {EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE};
  EGLContext c = eglCreateContext(d, cfg, EGL_NO_CONTEXT, xa);
  if (c == EGL_NO_CONTEXT) {
    printf("error=create_context egl_error=0x%x\n", eglGetError());
    return 1;
  }
  if (!eglMakeCurrent(d, EGL_NO_SURFACE, EGL_NO_SURFACE, c)) {
    printf("error=make_current egl_error=0x%x\n", eglGetError());
    return 1;
  }

  GLuint t, f;
  unsigned char px[4] = {0};
  glGenTextures(1, &t);
  glBindTexture(GL_TEXTURE_2D, t);
  glTexStorage2D(GL_TEXTURE_2D, 1, GL_RGBA8, 64, 64);
  glGenFramebuffers(1, &f);
  glBindFramebuffer(GL_FRAMEBUFFER, f);
  glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t, 0);
  GLenum status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
  glClearColor(1, 0, 0, 1);
  glClear(GL_COLOR_BUFFER_BIT);
  glReadPixels(0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, px);

  printf("gl_version=%s\n", str_or_null((const char *)glGetString(GL_VERSION)));
  printf("gl_renderer=%s\n", str_or_null((const char *)glGetString(GL_RENDERER)));
  printf("gl_vendor=%s\n", str_or_null((const char *)glGetString(GL_VENDOR)));
  printf("fbo_status=0x%x\n", status);
  printf("pixel=%d,%d,%d,%d\n", px[0], px[1], px[2], px[3]);
  printf("gl_error=0x%x\n", glGetError());

  eglMakeCurrent(d, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
  eglDestroyContext(d, c);
  eglTerminate(d);
  return !(status == GL_FRAMEBUFFER_COMPLETE && px[0] == 255 && px[1] == 0 && px[2] == 0 &&
           px[3] == 255);
}

static int drm_name(const char *node) {
  int fd = open(node, O_RDWR | O_CLOEXEC);
  if (fd < 0) {
    printf("error=open errno=%s\n", strerror(errno));
    return 1;
  }
  drmVersionPtr v = drmGetVersion(fd);
  if (!v) {
    printf("error=drm_get_version\n");
    close(fd);
    return 1;
  }
  printf("drm_driver=%.*s\n", v->name_len, v->name);
  printf("drm_version=%d.%d.%d\n", v->version_major, v->version_minor, v->version_patchlevel);
  drmFreeVersion(v);
  close(fd);
  return 0;
}

int main(int argc, char **argv) {
  setvbuf(stdout, NULL, _IOLBF, 0);
  if (argc < 2) {
    fprintf(stderr, "usage: eglprobe drm|gbm <node> | surfaceless | default\n");
    return 2;
  }
  const char *mode = argv[1];

  if (strcmp(mode, "drm") == 0 && argc > 2)
    return drm_name(argv[2]);

  printf("client_extensions=%s\n", str_or_null(eglQueryString(EGL_NO_DISPLAY, EGL_EXTENSIONS)));

  if (strcmp(mode, "surfaceless") == 0)
    return render(eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL));
  if (strcmp(mode, "default") == 0)
    return render(eglGetDisplay(EGL_DEFAULT_DISPLAY));
  if (strcmp(mode, "gbm") == 0 && argc > 2) {
    int fd = open(argv[2], O_RDWR | O_CLOEXEC);
    if (fd < 0) {
      printf("error=open errno=%s\n", strerror(errno));
      return 1;
    }
    struct gbm_device *g = gbm_create_device(fd);
    if (!g) {
      printf("error=gbm_create_device\n");
      return 1;
    }
    printf("gbm_backend=%s\n", str_or_null(gbm_device_get_backend_name(g)));
    int rc = render(eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, g, NULL));
    gbm_device_destroy(g);
    close(fd);
    return rc;
  }
  fprintf(stderr, "unknown mode %s\n", mode);
  return 2;
}
