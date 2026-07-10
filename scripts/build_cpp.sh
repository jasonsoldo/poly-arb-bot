#!/usr/bin/env bash
set -euo pipefail

mkdir -p build

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ \
  cpp/pnl_curve_engine/pnl_curve_engine.cpp \
  -o build/pnl_curve_engine

echo "built build/pnl_curve_engine"
