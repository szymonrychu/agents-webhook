#!/usr/bin/env bash
# Print the chart version to publish: chart-version.sh <ref_type> <ref_name> <sha>
# exit 0 version printed (main only), 1 no version in Chart.yaml, 2 nothing to publish.
set -euo pipefail

ref_type="${1:-}"
ref_name="${2:-}"
sha="${3:-}"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="$(sed -n 's/^version:[[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}[[:space:]]*$/\1/p' "$root/chart/agents-webhook/Chart.yaml" | head -n1)"
if [ -z "$version" ]; then
  echo "no version in chart/agents-webhook/Chart.yaml" >&2
  exit 1
fi

if [ "$ref_type" = "branch" ] && [ "$ref_name" = "main" ]; then
  echo "${version}-main.${sha:0:7}"
else
  echo "nothing to publish for $ref_type $ref_name" >&2
  exit 2
fi
