#!/bin/bash
# Double-click in Finder to build "Confidence.app" and a .dmg into dist/.
cd "$(dirname "$0")" || exit 1
if packaging/build_macos.sh; then
  open dist
else
  echo; read -n1 -p "Build failed (see above). Press any key to close."
fi
