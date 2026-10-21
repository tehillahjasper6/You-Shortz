#!/bin/bash
# Prepares a Claude Code cloud session to run the Viral Clipper pipeline:
# Node deps (Remotion + bundled FFmpeg), Python deps, the face-detector model,
# and a local Chromium for Remotion when the container provides one.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

npm install --no-audit --no-fund
python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt

# Face-detector weights (~5 MB, checksum-verified). Non-fatal: 04a retries on first use.
python3 - <<'PY' || echo "face model prefetch failed; it will be fetched on first use" >&2
import importlib.util, sys
sys.path.insert(0, "scripts")
spec = importlib.util.spec_from_file_location("face_track", "scripts/04a_face_track.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ensure_model()
PY

# Remotion downloads its own headless Chrome by default; reuse the preinstalled
# Playwright build instead when there is one (the download host may be blocked).
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  for shell in /opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell; do
    if [ -x "$shell" ]; then
      echo "export REMOTION_BROWSER_EXECUTABLE=\"$shell\"" >> "$CLAUDE_ENV_FILE"
      break
    fi
  done
fi
