#!/usr/bin/env bash
set -euo pipefail

mkdir -p build

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS \
  cpp/reference_ipc/reference_snapshot_test.cpp \
  -o build/reference_snapshot_test
./build/reference_snapshot_test
echo "built and tested build/reference_snapshot_test"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY \
  cpp/reference_ipc/latest_value_server_test.cpp \
  -o build/latest_value_server_test -lpthread
./build/latest_value_server_test
echo "built and tested build/latest_value_server_test"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY \
  cpp/reference_ipc/latest_value_client_test.cpp \
  -o build/latest_value_client_test -lpthread
./build/latest_value_client_test
echo "built and tested build/latest_value_client_test"

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
  g++ -std=c++17 -O3 -Wall -Wextra cpp/reference_price_engine/reference_price_engine.cpp \
    -o build/reference_price_engine -lboost_system -lssl -lcrypto -lpthread
  echo "built build/reference_price_engine"
else
  echo "skip market_ws_engine: install libboost-system-dev libssl-dev"
fi
