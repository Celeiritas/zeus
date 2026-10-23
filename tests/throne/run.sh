#!/bin/bash
# tests/throne/run.sh <argus-ui checkout>: the Throne page in a real browser, under Elaia, against a fake zeus.
# It copies plugin/ and throne.test.js into that checkout's tests/, so give it a throwaway checkout (CI does: the
# throne-page job in .github/workflows/tests.yml). Needs what argus-ui's tests/run.sh needs: python3, node, Playwright.
set -e
R=$(cd "$(dirname "$0")/../.." && pwd); A=$(cd "${1:?usage: bash tests/throne/run.sh <argus-ui checkout>}" && pwd)
T=$(mktemp -d)
mkdir -p "$T/data/queue/.done" "$T/data/state" "$T/data/log"
echo '{"version": 1, "repos": {"you/argus-ui": {}, "you/hermes": {}}}' > "$T/repos.json"
rm -rf "$A/tests/plugin-fixtures/throne"; cp -r "$R/plugin" "$A/tests/plugin-fixtures/throne"
cp "$R/tests/throne/throne.test.js" "$A/tests/throne.test.js"
chmod +x "$R/tests/throne/fake_zeus.py"
export ZEUS_DATA="$T/data" ZEUS_CONFIG="$T/repos.json" ZEUS_ALERT="$T/alert.json" ZEUS_BIN="$R/tests/throne/fake_zeus.py"
bash "$A/tests/run.sh" throne
