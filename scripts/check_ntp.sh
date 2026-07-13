#!/usr/bin/env bash
set -euo pipefail

if [[ "$(timedatectl show -p NTPSynchronized --value)" != "yes" ]]; then
  echo "NTP_NOT_SYNCHRONIZED" >&2
  exit 1
fi

echo "NTP_SYNCHRONIZED"
