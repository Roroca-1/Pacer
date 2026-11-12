#!/bin/sh
# usage: manifest.sh /abs/path/libVkLayer_pacer.so LAYER_NAME
cat <<JSON
{
  "file_format_version": "1.0.0",
  "layer": {
    "name": "$2",
    "type": "GLOBAL",
    "api_version": "1.3.0",
    "library_path": "$1",
    "implementation_version": "${PACER_VERSION:-1}",
    "description": "pacer: frame limiter with presets, in-game hotkeys and OSD",
    "functions": {
      "vkNegotiateLoaderLayerInterfaceVersion": "vkNegotiateLoaderLayerInterfaceVersion"
    },
    "disable_environment": {
      "DISABLE_PACER": "1"
    }
  }
}
JSON
