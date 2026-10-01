#!/usr/bin/env bash
# Check upstream OKF spec repo for recent commits.
# Usage: bash tools/check-okf-upstream.sh
#
# The spec lives in its own repo since 2026-08-21 (moved out of
# GoogleCloudPlatform/knowledge-catalog, where the okf/ directory
# was archived with a pointer to this repo).
set -euo pipefail

REPO="GoogleCloudPlatform/open-knowledge-format"

curl -sL "https://api.github.com/repos/${REPO}/commits?per_page=5" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if isinstance(data, dict) and 'message' in data:
    print(data.get('message', 'Unknown') + ' (rate limited or requires auth)')
    sys.exit(1)
for c in data:
    date = c['commit']['author']['date'][:10]
    msg = c['commit']['message'].split('\n')[0]
    sha = c['sha'][:7]
    print(f'{date} {sha} {msg}')
"
