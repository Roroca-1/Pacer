CXX ?= g++
CXXFLAGS ?= -O2 -g
CXXFLAGS += -std=c++17 -fPIC -fvisibility=hidden -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers
LDFLAGS += -shared -Wl,--no-undefined -Wl,-z,nodelete -pthread -ldl

SRC := layer/src
CORE := $(SRC)/core.cpp
HDRS := $(wildcard $(SRC)/*.h)

all: build/x86_64/libVkLayer_pacer.so build/x86_64/libpacer_gl.so \
     build/i386/libVkLayer_pacer.so build/i386/libpacer_gl.so build/tests/pacing_test

build/x86_64/libVkLayer_pacer.so: $(SRC)/vk_layer.cpp $(CORE) $(HDRS)
	@mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) -m64 -o $@ $(SRC)/vk_layer.cpp $(CORE) $(LDFLAGS)

build/x86_64/libpacer_gl.so: $(SRC)/gl_hook.cpp $(CORE) $(HDRS)
	@mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) -m64 -o $@ $(SRC)/gl_hook.cpp $(CORE) $(LDFLAGS)

build/i386/libVkLayer_pacer.so: $(SRC)/vk_layer.cpp $(CORE) $(HDRS)
	@mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) -m32 -o $@ $(SRC)/vk_layer.cpp $(CORE) $(LDFLAGS) -latomic

build/i386/libpacer_gl.so: $(SRC)/gl_hook.cpp $(CORE) $(HDRS)
	@mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) -m32 -o $@ $(SRC)/gl_hook.cpp $(CORE) $(LDFLAGS) -latomic

build/tests/pacing_test: tests/pacing_test.cpp $(CORE) $(HDRS)
	@mkdir -p $(@D)
	$(CXX) $(filter-out -fvisibility=hidden,$(CXXFLAGS)) -I$(SRC) -o $@ tests/pacing_test.cpp $(CORE) -pthread -ldl

x86_64: build/x86_64/libVkLayer_pacer.so build/x86_64/libpacer_gl.so build/tests/pacing_test

clean:
	rm -rf build

VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' gui/pacer/__init__.py)

# headless checks: pacing algorithm (synthetic load), Python syntax, offscreen GUI layout
test: build/tests/pacing_test
	build/tests/pacing_test
	python3 -m py_compile gui/pacer/*.py
	python3 tests/gui_layout.py

install: all
	./install.sh

uninstall:
	./uninstall.sh

# source tarball for releases / packaging
dist:
	@mkdir -p dist
	git archive --format=tar.gz --prefix=pacer-$(VERSION)/ -o dist/pacer-$(VERSION).tar.gz HEAD
	@echo dist/pacer-$(VERSION).tar.gz

.PHONY: all clean x86_64 test install uninstall dist dev

build/tests/vk_smoke: tests/vk_smoke.cpp
	@mkdir -p $(@D)
	$(CXX) -O1 -g -std=c++17 -o $@ $< -lvulkan

build/dev-manifest/pacer.json: build/x86_64/libVkLayer_pacer.so
	@mkdir -p $(@D)
	./packaging/manifest.sh $(abspath build/x86_64/libVkLayer_pacer.so) VK_LAYER_PACER_limiter_dev > $@

dev: all build/tests/vk_smoke build/dev-manifest/pacer.json

build/tests/gl_smoke: tests/gl_smoke.c
	@mkdir -p $(@D)
	$(CC) -O1 -o $@ $< -lEGL -lGL

dev: build/tests/gl_smoke
