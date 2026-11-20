#!/usr/bin/env bash
set -Eeuo pipefail
source tests/lc4ri/guests/guest-image.env
image_dir=$1
mkdir -p "$image_dir"
image_path="$image_dir/$CWH_E2E_GUEST_IMAGE"
if [[ ! -f "$image_path" ]]; then
  if command -v aria2c >/dev/null; then
    aria2c --max-connection-per-server=8 --split=8 --min-split-size=16M \
      --file-allocation=none --summary-interval=60 --console-log-level=warn \
      --dir="$image_dir" --out="$CWH_E2E_GUEST_IMAGE.download" \
      "$CWH_E2E_GUEST_IMAGE_BASE_URL/$CWH_E2E_GUEST_IMAGE"
  else
    curl --fail --silent --show-error --location --retry 3 \
      "$CWH_E2E_GUEST_IMAGE_BASE_URL/$CWH_E2E_GUEST_IMAGE" -o "$image_path.download"
  fi
  printf '%s  %s\n' "$CWH_E2E_GUEST_IMAGE_SHA256" "$image_path.download" | sha256sum -c -
  mv "$image_path.download" "$image_path"
fi
printf '%s  %s\n' "$CWH_E2E_GUEST_IMAGE_SHA256" "$image_path" | sha256sum -c -
