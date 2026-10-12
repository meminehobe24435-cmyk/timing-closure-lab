#!/usr/bin/env bash
# tools/oss.sh -- POSIX counterpart of tools/oss.bat.
#
# Every EDA invocation in this project goes through one of these two wrappers so
# that the local Windows run and the Linux CI run resolve the same tools the
# same way. The wrapper tries, in order:
#
#   1. $TCLAB_OSS_ENV      explicit path to an oss-cad-suite environment.sh
#   2. $OSS_CAD_SUITE      root of an oss-cad-suite install
#   3. /opt/oss-cad-suite, /usr/local/oss-cad-suite
#   4. whatever is already on PATH (e.g. apt-installed yosys/nextpnr/iverilog)
#
# Usage:  tools/oss.sh yosys -V
set -u

_found=""

try_env() {
    local f="$1"
    if [ -n "$f" ] && [ -f "$f" ]; then
        # shellcheck disable=SC1090
        . "$f" >/dev/null 2>&1 && _found="$f"
    fi
}

try_env "${TCLAB_OSS_ENV:-}"
[ -n "$_found" ] || try_env "${OSS_CAD_SUITE:-}/environment.sh"
[ -n "$_found" ] || try_env "/opt/oss-cad-suite/environment.sh"
[ -n "$_found" ] || try_env "/usr/local/oss-cad-suite/environment.sh"

exec "$@"
