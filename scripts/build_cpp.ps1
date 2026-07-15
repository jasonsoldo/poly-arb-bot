$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path "build" | Out-Null

g++ -std=c++17 -O3 -Wall -Wextra -DBOOST_BIND_GLOBAL_PLACEHOLDERS `
  cpp/reference_ipc/reference_snapshot_test.cpp `
  -o build/reference_snapshot_test.exe
& .\build\reference_snapshot_test.exe
Write-Host "built and tested build/reference_snapshot_test.exe"

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ `
  cpp/pnl_curve_engine/pnl_curve_engine.cpp `
  -o build/pnl_curve_engine.exe

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ `
  cpp/market_engine/market_engine.cpp `
  -o build/market_engine.exe

Write-Host "built build/pnl_curve_engine.exe"
