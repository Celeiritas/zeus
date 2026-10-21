#!/bin/bash
# The scripts/deploy.sh that the test repositories carry (tests/test_deploy.py commits it). It honours the contract in
# docs/deploy-script.md: --no-pull, ZEUS_STAGE=install before the install, ZEUS_ID/ZEUS_SHA/ZEUS_PREVIOUS in the
# environment. FAKE_DEPLOY chooses a failure: fail-build | fail-install | hang. The "install" writes DEPLOY_MARKER.
set -e
# FAKE_DEPLOY_ONLY=<version>: the failure applies to that version only (the restore of the previous one then works)
[ -n "$FAKE_DEPLOY_ONLY" ] && [ "$(cat VERSION)" != "$FAKE_DEPLOY_ONLY" ] && FAKE_DEPLOY=
[ "$1" = "--no-pull" ] || { echo "deploy.sh: expected --no-pull, got '$1'" >&2; exit 9; }
echo "build $ZEUS_SHA"
[ "$FAKE_DEPLOY" = fail-build ] && { echo "build failed" >&2; exit 3; }
echo ZEUS_STAGE=install
[ "$FAKE_DEPLOY" = fail-install ] && { echo "install failed" >&2; exit 4; }
[ "$FAKE_DEPLOY" = hang ] && sleep 30
echo "$ZEUS_SHA $ZEUS_ID $ZEUS_PREVIOUS $(cat VERSION)" > "$DEPLOY_MARKER"
echo "installed"
