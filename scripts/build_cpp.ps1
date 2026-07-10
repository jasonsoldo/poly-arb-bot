$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path "build" | Out-Null

g++ -std=c++17 -O3 -Wall -Wextra -static -static-libgcc -static-libstdc++ `
  cpp/pnl_curve_engine/pnl_curve_engine.cpp `
  -o build/pnl_curve_engine.exe

Write-Host "built build/pnl_curve_engine.exe"
