#!/usr/bin/env bash
# Install one official FreeCAD release for the real-hardware CI job and export a
# verified `FreeCADCmd` path as FREECAD_TEST_EXECUTABLE.
#
# The AppImage is only downloaded when the local copy is missing or fails its
# published SHA256, so a warm actions/cache entry skips the download entirely.
# A candidate binary is accepted only after it has actually executed this
# repository's FreeCAD driver, which proves the whole invocation path works and
# reports the expected FreeCAD series before the test suite is allowed to run.
set -euo pipefail

version="${FREECAD_VERSION:?FREECAD_VERSION must be set}"
asset="${FREECAD_ASSET:?FREECAD_ASSET must be set}"
expected_series="${version%.*}"

workdir="${FREECAD_CACHE_DIR:-${RUNNER_TEMP:-/tmp}/freecad-${version}}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
driver="$(cd "${script_dir}/../.." && pwd)/src/dcc_mcp_freecad/freecad_driver.py"
url="https://github.com/FreeCAD/FreeCAD/releases/download/${version}/${asset}"
sha_asset="${asset}-SHA256.txt"

mkdir -p "${workdir}"
cd "${workdir}"

sha_matches() {
  [ -f "${asset}" ] && [ -f "${sha_asset}" ] || return 1
  [ "$(awk '{print $1}' "${sha_asset}")" = "$(sha256sum "${asset}" | awk '{print $1}')" ]
}

if sha_matches; then
  echo "Reusing cached ${asset}"
else
  echo "::group::Download ${asset}"
  rm -f "${asset}"
  curl -fsSL --retry 3 -o "${asset}" "${url}"
  curl -fsSL --retry 3 -o "${sha_asset}" "${url}-SHA256.txt"
  echo "::endgroup::"
  if ! sha_matches; then
    echo "::error::SHA256 mismatch for ${asset}"
    exit 1
  fi
fi
echo "Verified ${asset} (sha256 $(awk '{print $1}' "${sha_asset}"))"

root="${workdir}/squashfs-root"
if [ ! -d "${root}" ]; then
  echo "::group::Extract ${asset}"
  chmod +x "${asset}"
  "./${asset}" --appimage-extract >/dev/null
  echo "::endgroup::"
fi

mapfile -t candidates < <(
  find "${root}" \( -type f -o -type l \) \
    \( -name 'FreeCADCmd' -o -name 'freecadcmd' \) -perm -u+x | sort
)
if [ -e "${root}/AppRun" ] && [ ! -e "${root}/FreeCADCmd" ]; then
  # AppImages dispatch on argv[0], so expose AppRun under the console name.
  ln -s AppRun "${root}/FreeCADCmd"
  candidates+=("${root}/FreeCADCmd")
fi
if [ "${#candidates[@]}" -eq 0 ]; then
  echo "::error::No FreeCADCmd candidate inside ${root}"
  find "${root}" -maxdepth 3 | sort | head -50
  exit 1
fi

# Runs the repository driver through the candidate; prints the FreeCAD version.
probe() {
  local executable="$1"
  local probe_dir version
  probe_dir="$(mktemp -d)"
  printf '{"method": "system.status", "params": {}}' > "${probe_dir}/request.json"
  if ! timeout 300 "${executable}" --safe-mode --user-cfg "${probe_dir}/user.cfg" \
    "${driver}" --pass "${probe_dir}/request.json" "${probe_dir}/result.json" \
    > "${probe_dir}/stdout.log" 2> "${probe_dir}/stderr.log"; then
    echo "candidate exited non-zero: ${executable}"
    tail -20 "${probe_dir}/stderr.log"
    return 1
  fi
  if [ ! -f "${probe_dir}/result.json" ]; then
    echo "candidate produced no result file: ${executable}"
    tail -20 "${probe_dir}/stderr.log"
    return 1
  fi
  if ! version="$(python3 "${script_dir}/freecad-version.py" "${probe_dir}/result.json")"; then
    echo "candidate driver probe failed: ${executable}"
    return 1
  fi
  printf '%s' "${version}"
}

for candidate in "${candidates[@]}"; do
  echo "Probing ${candidate}"
  if ! reported="$(probe "${candidate}")"; then
    continue
  fi
  if [[ "${reported}" != "${expected_series}".* ]]; then
    echo "candidate reported FreeCAD ${reported}, expected the ${expected_series}.x series"
    continue
  fi
  echo "Resolved FreeCADCmd ${candidate} (FreeCAD ${reported})"
  {
    echo "FREECAD_TEST_EXECUTABLE=${candidate}"
    echo "FREECAD_REAL_VERSION=${reported}"
  } >> "${GITHUB_ENV:?GITHUB_ENV must be set}"
  exit 0
done

echo "::error::No candidate FreeCADCmd could run the repository driver"
exit 1
