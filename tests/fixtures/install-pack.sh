#!/bin/bash
# A fake install script for a staged pack (tests/test_staged.py; the real one is examples/install-pack.sh.example).
# It writes what Zeus passed it, one line per run, into INSTALL_MARKER. FAKE_INSTALL chooses a failure for the install
# mode: fail-before (exit before ZEUS_STAGE=install) | fail-after (exit after it). FAKE_ROLLBACK=fail does the same for
# the rollback mode.
set -e
mode=$1
if [ "$mode" = install ]; then
  bytes=$(wc -c < "$2" | tr -d ' ')
  echo "install $3 $4 bytes=$bytes files=$ZEUS_PACK_FILES sha256=$ZEUS_PACK_SHA256 prev=$ZEUS_PREVIOUS prevver=$ZEUS_PREVIOUS_VERSION pack=$2" >> "$INSTALL_MARKER"
  [ "$FAKE_INSTALL" = fail-before ] && { echo "check failed" >&2; exit 3; }
  echo ZEUS_STAGE=install
  [ "$FAKE_INSTALL" = fail-after ] && { echo "swap failed" >&2; exit 4; }
elif [ "$mode" = rollback ]; then
  echo "rollback $2 $3 prev=$ZEUS_PREVIOUS" >> "$INSTALL_MARKER"
  echo ZEUS_STAGE=install
  [ "$FAKE_ROLLBACK" = fail ] && { echo "rollback failed" >&2; exit 5; }
else
  echo "unknown mode $mode" >&2; exit 9
fi
echo "done $mode"
