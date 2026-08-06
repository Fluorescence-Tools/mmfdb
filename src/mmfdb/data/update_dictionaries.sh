#!/usr/bin/env bash
# Download/update the upstream mmCIF dictionary files from wwPDB.
# Run from any directory — files are placed next to this script.
#
# Usage:
#   bash src/mmfdb/data/update_dictionaries.sh          # check for drift
#   bash src/mmfdb/data/update_dictionaries.sh --accept # take the new revisions
#
# Why this is not a plain `curl -o`:
#
# The vocabulary MMFDB validates against, and that every written artifact is
# tagged with, is defined by these files. Overwriting them in place means a
# wwPDB revision can rename or withdraw an item and the only visible effect is
# that reconcile_schema quietly adds a column. So the default run **downloads
# to a temporary file and compares**: it reports what changed and changes
# nothing. `--accept` is the deliberate act of taking a new revision, and it
# updates dictionaries.lock in the same step so the pin never drifts from the
# files it pins.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BASE_URL="https://mmcif.wwpdb.org/dictionaries/ascii"
LOCKFILE="dictionaries.lock"

DICTS=(
    # Core PDBx/mmCIF
    "mmcif_pdbx_v50.dic"          # PDBx/mmCIF v5 (current stable)
    "mmcif_pdbx_v5_next.dic"      # PDBx/mmCIF v5-next (development)
    "mmcif_std.dic"               # Original mmCIF standard
    "mmcif_ddl.dic"               # DDL v2 (dictionary definition language)

    # Extensions
    "mmcif_ihm_ext.dic"           # IHM (integrative/hybrid modeling)
    "mmcif_ihm_flr_ext.dic"       # flrCIF (fluorescence/FRET)
    "mmcif_ma.dic"                # ModelCIF (computed structure models)
)

ACCEPT=0
[[ "${1:-}" == "--accept" ]] && ACCEPT=1

sha256_of() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

# `_dictionary.version` sits in the data block, outside every save frame.
version_of() {
    awk '/^[[:space:]]*_dictionary\.version/ { print $2; exit }' "$1"
}

locked_field() {   # locked_field <name> <field-index>
    [[ -f "$LOCKFILE" ]] || return 0
    awk -v n="$1" -v f="$2" '$1 == n { print $f }' "$LOCKFILE"
}

TMPDIR_DL="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_DL"' EXIT

echo "Source: $BASE_URL"
if [[ $ACCEPT -eq 1 ]]; then
    echo "Mode:   --accept (files and $LOCKFILE will be updated)"
else
    echo "Mode:   check only (nothing will be written; pass --accept to take changes)"
fi
echo ""

changed=0
failed=0
declare -a NEW_LOCK=()

for dic in "${DICTS[@]}"; do
    printf "  %-28s ... " "$dic"
    if ! curl -sf -o "$TMPDIR_DL/$dic" "$BASE_URL/$dic"; then
        printf "FAILED (download)\n"
        failed=$((failed + 1))
        # Keep whatever the lockfile already says, so one network failure
        # cannot silently drop a pin.
        if [[ -f "$dic" ]]; then
            NEW_LOCK+=("$dic $(version_of "$dic") $(sha256_of "$dic")")
        fi
        continue
    fi

    new_sha="$(sha256_of "$TMPDIR_DL/$dic")"
    new_ver="$(version_of "$TMPDIR_DL/$dic")"
    old_sha="$(locked_field "$dic" 3)"
    old_ver="$(locked_field "$dic" 2)"

    if [[ -z "$old_sha" ]]; then
        printf "NEW (v%s)\n" "${new_ver:-?}"
        changed=$((changed + 1))
    elif [[ "$new_sha" == "$old_sha" ]]; then
        printf "unchanged (v%s)\n" "${new_ver:-?}"
    else
        printf "CHANGED  v%s -> v%s\n" "${old_ver:-?}" "${new_ver:-?}"
        printf "  %-28s     %s\n" "" "${old_sha:0:16}… -> ${new_sha:0:16}…"
        changed=$((changed + 1))
    fi

    if [[ $ACCEPT -eq 1 ]]; then
        cp "$TMPDIR_DL/$dic" "$dic"
    fi
    NEW_LOCK+=("$dic $new_ver $new_sha")
done

echo ""

if [[ $ACCEPT -eq 1 ]]; then
    {
        echo "# Upstream mmCIF dictionaries this tree is pinned to."
        echo "# Written by update_dictionaries.sh --accept. Columns: name version sha256"
        printf '%s\n' "${NEW_LOCK[@]}"
    } > "$LOCKFILE"
    echo "Updated $LOCKFILE and ${#NEW_LOCK[@]} dictionary file(s)."
    echo "Re-run the test suite: a renamed or withdrawn item is a real change."
elif [[ $changed -gt 0 ]]; then
    echo "$changed dictionary/dictionaries differ from $LOCKFILE. Nothing was written."
    echo "Review the change, then re-run with --accept to take it."
    exit 1
else
    echo "All dictionaries match $LOCKFILE."
fi

[[ $failed -gt 0 ]] && echo "WARNING: $failed download(s) failed." >&2
exit 0
