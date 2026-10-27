#!/bin/sh
# Update the installed NetWatch with the sources in this folder (keeps your history and settings).
# After this first update the command `netwatch-update` is available everywhere.
exec sh "$(dirname "$0")/packaging/netwatch-update" "$@" "$(cd "$(dirname "$0")" && pwd)"
