$ErrorActionPreference = "Stop"

function Assert-NativeSuccess([string]$Step) {
  if ($LASTEXITCODE -ne 0) {
    throw "$Step failed with exit code $LASTEXITCODE"
  }
}

New-Item -ItemType Directory -Force -Path "build" | Out-Null

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS `
  cpp/reference_ipc/reference_snapshot_test.cpp `
  -o build/reference_snapshot_test.exe
Assert-NativeSuccess "compile reference_snapshot_test"
& .\build\reference_snapshot_test.exe
Assert-NativeSuccess "run reference_snapshot_test"
Write-Host "built and tested build/reference_snapshot_test.exe"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY `
  cpp/reference_ipc/latest_value_server_test.cpp `
  -o build/latest_value_server_test.exe -lws2_32 -lmswsock
Assert-NativeSuccess "compile latest_value_server_test"
& .\build\latest_value_server_test.exe
Assert-NativeSuccess "run latest_value_server_test"
Write-Host "built and tested build/latest_value_server_test.exe"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY `
  cpp/reference_ipc/latest_value_client_test.cpp `
  -o build/latest_value_client_test.exe -lws2_32 -lmswsock
Assert-NativeSuccess "compile latest_value_client_test"
& .\build\latest_value_client_test.exe
Assert-NativeSuccess "run latest_value_client_test"
Write-Host "built and tested build/latest_value_client_test.exe"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS `
  cpp/strategy/ev_strategy_test.cpp `
  -o build/ev_strategy_test.exe
Assert-NativeSuccess "compile ev_strategy_test"
& .\build\ev_strategy_test.exe
Assert-NativeSuccess "run ev_strategy_test"
Write-Host "built and tested build/ev_strategy_test.exe"

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ `
  cpp/pnl_curve_engine/pnl_curve_engine.cpp `
  -o build/pnl_curve_engine.exe
Assert-NativeSuccess "compile pnl_curve_engine"

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ `
  cpp/market_engine/market_engine.cpp `
  -o build/market_engine.exe
Assert-NativeSuccess "compile market_engine"

Write-Host "built build/pnl_curve_engine.exe"

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY `
  cpp/market_ws_engine/market_ws_engine.cpp `
  -o build/market_ws_engine.exe -lssl -lcrypto -lws2_32 -lcrypt32 -lmswsock
Assert-NativeSuccess "compile market_ws_engine"
Write-Host "built build/market_ws_engine.exe"

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY `
  cpp/reference_price_engine/reference_price_engine.cpp `
  -o build/reference_price_engine.exe -lssl -lcrypto -lws2_32 -lcrypt32 -lmswsock
Assert-NativeSuccess "compile reference_price_engine"
Write-Host "built build/reference_price_engine.exe"
