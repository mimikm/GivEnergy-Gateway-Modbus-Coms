#!/usr/bin/env bash
# Bump the manifest version, tag and push, then create the GitHub release.
# Usage: scripts/release.sh 0.1.2
set -euo pipefail

VERSION="${1:?usage: scripts/release.sh X.Y.Z}"
MANIFEST="custom_components/givenergy_modbus/manifest.json"

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([a-z0-9.-]*)?$ ]] || { echo "Version must look like 1.2.3"; exit 1; }
git diff --quiet || { echo "Commit or stash your changes first"; exit 1; }

python3 - "$VERSION" "$MANIFEST" <<'PY'
import json, sys
version, path = sys.argv[1], sys.argv[2]
data = json.load(open(path))
data["version"] = version
json.dump(data, open(path, "w"), indent=2)
open(path, "a").write("\n")
PY

git add "$MANIFEST"
git commit -m "Release v$VERSION"
git tag "v$VERSION"
git push origin HEAD "v$VERSION"

if command -v gh >/dev/null 2>&1; then
  gh release create "v$VERSION" --title "v$VERSION" --generate-notes
  echo "Release created — the Release workflow will attach givenergy_modbus.zip."
else
  echo "Tag pushed. Create the release for v$VERSION on GitHub (Releases → Draft a new release)."
fi
