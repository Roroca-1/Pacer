/* Headless EGL pbuffer loop; run under LD_PRELOAD=libpacer_gl.so to test the GL hook.
   Prints average fps over each 1 s window for N seconds (argv[1], default 4). */
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GL/gl.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec*1e-9;}
int main(int argc,char**argv){
  int secs=argc>1?atoi(argv[1]):4;
  PFNEGLGETPLATFORMDISPLAYEXTPROC gpd=(PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
  EGLDisplay d=gpd?gpd(EGL_PLATFORM_SURFACELESS_MESA,EGL_DEFAULT_DISPLAY,NULL):EGL_NO_DISPLAY;
  if(d==EGL_NO_DISPLAY) d=eglGetDisplay(EGL_DEFAULT_DISPLAY);
  if(!eglInitialize(d,0,0)){puts("eglInitialize failed");return 1;}
  EGLint ca[]={EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_RENDERABLE_TYPE,EGL_OPENGL_BIT,EGL_NONE};
  EGLConfig c;EGLint n=0;eglChooseConfig(d,ca,&c,1,&n);
  if(!n){puts("no config");return 1;}
  EGLint pa[]={EGL_WIDTH,64,EGL_HEIGHT,64,EGL_NONE};
  EGLSurface s=eglCreatePbufferSurface(d,c,pa);
  eglBindAPI(EGL_OPENGL_API);
  EGLContext x=eglCreateContext(d,c,EGL_NO_CONTEXT,NULL);
  if(!eglMakeCurrent(d,s,s,x)){puts("makecurrent failed");return 1;}
  printf("renderer: %s\n",glGetString(GL_RENDERER));
  double t0=now(),w0=t0;int frames=0;
  while(now()-t0<secs){
    glClearColor(frames%2,0,0,1);glClear(GL_COLOR_BUFFER_BIT);glFinish();
    eglSwapBuffers(d,s);frames++;
    double t=now(); if(t-w0>=1.0){printf("fps %.2f\n",frames/(t-w0));fflush(stdout);frames=0;w0=t;}
  }
  return 0;
}
