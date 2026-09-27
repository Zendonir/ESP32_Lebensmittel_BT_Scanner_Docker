#!/usr/bin/env bash
# Naechste Versionsnummer aus dem juengsten Tag v*.
#
# Aufruf: naechste-version.sh [patch|minor|major]   (Vorgabe: patch)
# Ausgabe: der neue Tag, z. B. v1.0.3
#
# Gibt es noch keinen Tag, ist die erste Fassung v1.0.0 - unabhaengig von der
# Stufe. Tags, die nicht wie vX.Y.Z aussehen, zaehlen nicht mit.
set -euo pipefail

stufe="${1:-patch}"
letzter="$(git tag -l 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)"

if [ -z "$letzter" ]; then
  echo "v1.0.0"
  exit 0
fi

IFS=. read -r major minor patch <<<"${letzter#v}"
case "$stufe" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
  *) echo "Unbekannte Stufe: $stufe (patch, minor oder major)" >&2; exit 2 ;;
esac
echo "v${major}.${minor}.${patch}"
