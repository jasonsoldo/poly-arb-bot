$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path "build" | Out-Null

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS `
  cpp/reference_ipc/reference_snapshot_test.cpp `
  -o build/reference_snapshot_test.exe
& .\build\reference_snapshot_test.exe
Write-Host "built and tested build/reference_snapshot_test.exe"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY `
  cpp/reference_ipc/latest_value_server_test.cpp `
  -o build/latest_value_server_test.exe -lws2_32 -lmswsock
& .\build\latest_value_server_test.exe
Write-Host "built and tested build/latest_value_server_test.exe"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS -DBOOST_ERROR_CODE_HEADER_ONLY `
  cpp/reference_ipc/latest_value_client_test.cpp `
  -o build/latest_value_client_test.exe -lws2_32 -lmswsock
& .\build\latest_value_client_test.exe
Write-Host "built and tested build/latest_value_client_test.exe"

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS `
  cpp/strategy/ev_strategy_test.cpp `
  -o build/ev_strategy_test.exe
Write-Host "built build/ev_strategy_test.exe"

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ `
  cpp/pnl_curve_engine/pnl_curve_engine.cpp `
  -o build/pnl_curve_engine.exe

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ `
  cpp/market_engine/market_engine.cpp `
  -o build/market_engine.exe

Write-Host "built build/pnl_curve_engine.exe"
