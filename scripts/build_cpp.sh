#!/usr/bin/env bash
set -euo pipefail

mkdir -p build

platform_network_libs=()
boost_compile_flags=()
boost_system_libs=(-lboost_system)
openssl_available=false
case "$(uname -s)" in
  MINGW*|MSYS*)
    platform_network_libs=(-lws2_32 -lmswsock -lcrypt32)
    if [[ -f /ucrt64/include/openssl/ssl.h && -f /ucrt64/lib/libssl.a ]]; then
      openssl_available=true
      boost_compile_flags=(-DBOOST_ERROR_CODE_HEADER_ONLY)
      boost_system_libs=()
    fi
    ;;
esac

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS \
  cpp/reference_ipc/reference_snapshot_test.cpp \
  -o build/reference_snapshot_test
./build/reference_snapshot_test
echo "built and tested build/reference_snapshot_test"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY \
  cpp/reference_ipc/latest_value_server_test.cpp \
  -o build/latest_value_server_test -lpthread "${platform_network_libs[@]}"
./build/latest_value_server_test
echo "built and tested build/latest_value_server_test"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY \
  cpp/reference_ipc/latest_value_client_test.cpp \
  -o build/latest_value_client_test -lpthread "${platform_network_libs[@]}"
./build/latest_value_client_test
echo "built and tested build/latest_value_client_test"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS \
  cpp/strategy/ev_strategy_test.cpp \
  -o build/ev_strategy_test
strategy_smoke='{"mode":"probability","settlement_reference":101,"price_to_beat":100,"seconds_to_close":60,"volatility_per_sqrt_second":0.001,"model_sample_count":60,"model_sample_span_seconds":60,"momentum_bps_30s":1,"paired_book_imbalance":0}'
strategy_output="$(printf '%s\n' "$strategy_smoke" | ./build/ev_strategy_test)"
grep -Eq '"estimated_probability":[0-9]' <<< "$strategy_output"
echo "built and tested build/ev_strategy_test"

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ \
  cpp/pnl_curve_engine/pnl_curve_engine.cpp \
  -o build/pnl_curve_engine

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ \
  cpp/market_engine/market_engine.cpp \
  -o build/market_engine

echo "built build/pnl_curve_engine"

if command -v pkg-config >/dev/null && pkg-config --exists openssl; then
  openssl_available=true
fi

if [[ "$openssl_available" == true ]]; then
  g++ -std=c++17 -O3 -Wall -Wextra "${boost_compile_flags[@]}" cpp/market_ws_engine/market_ws_engine.cpp \
    -o build/market_ws_engine "${boost_system_libs[@]}" -lssl -lcrypto -lpthread "${platform_network_libs[@]}"
  echo "built build/market_ws_engine"
  g++ -std=c++17 -O3 -Wall -Wextra "${boost_compile_flags[@]}" cpp/reference_price_engine/reference_price_engine.cpp \
    -o build/reference_price_engine "${boost_system_libs[@]}" -lssl -lcrypto -lpthread "${platform_network_libs[@]}"
  echo "built build/reference_price_engine"
else
  echo "skip market_ws_engine: install libboost-system-dev libssl-dev"
fi
