#!/usr/bin/env bash
set -euo pipefail

mkdir -p build

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ \
  cpp/pnl_curve_engine/pnl_curve_engine.cpp \
  -o build/pnl_curve_engine

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ \
  cpp/market_engine/market_engine.cpp \
  -o build/market_engine

echo "built build/pnl_curve_engine"

if command -v pkg-config >/dev/null && pkg-config --exists openssl; then
  g++ -std=c++17 -O3 -Wall -Wextra cpp/market_ws_engine/market_ws_engine.cpp \
    -o build/market_ws_engine -lboost_system -lssl -lcrypto -lpthread
  echo "built build/market_ws_engine"
else
  echo "skip market_ws_engine: install libboost-system-dev libssl-dev"
fi
