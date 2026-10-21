# What a repository's `scripts/deploy.sh` must honour
Zeus runs one script per repository, always the same path, always from the commit the owner approved: `scripts/deploy.sh` (fixed in code; no config can name another). [`examples/deploy.sh.example`](../examples/deploy.sh.example) is a template; argus-ui's own script (4.9.1 and later) is a real one.

## How Zeus runs it
1. **The clone is Zeus's.** `path` from `repos.json` (`~/src/<name>`). Zeus makes it on the first deploy (`git clone --no-checkout`), fetches `main` of the allowlisted url, checks the commit is on `main`, and checks it out **detached** with hooks off. A clone with local changes fails the checkout: nobody edits there. A tree with submodules, or without `scripts/deploy.sh`, is refused before anything runs.
2. **The command:** `bash scripts/deploy.sh --no-pull`, in the clone, stdin closed, stdout and stderr together into `~/.local/share/zeus/log/deploy-<id>.log` (the Throne tails it).
3. **The environment:** Zeus's own, plus
   | Variable | Value |
   |---|---|
   | `ZEUS_ID` | the request id, e.g. `20261021-090012-argus-ui-3fa9c1b` |
   | `ZEUS_SHA` | the commit being deployed (40 hex) |
   | `ZEUS_PREVIOUS` | the commit deployed before it, or empty on the first deploy |
4. **The time limit:** `deploy_timeout_s` (default 900 s). Past it the script and everything it started are stopped.
5. **Then health:** the repository's `health` from `repos.json`, tried until it passes or `health_wait_s` (default 30 s) runs out. An `http` check that answers JSON with a `version` must match the commit's `VERSION` file.

## What the script must do
- **Accept `--no-pull`** and then leave git alone: Zeus has already put the right commit in place. Without the flag it may `git pull --ff-only` as before, for the bootstrap and for machines without Zeus.
- **Print `ZEUS_STAGE=install` on its own line before the first change** to what runs. Everything before it (build, tests, packaging) must change nothing outside the clone; everything after it installs and restarts.
- **Exit non-zero on any failure** (`set -e`). Zeus decides from the exit code and the stage line:
  | Where it failed | What Zeus does |
  |---|---|
  | before `ZEUS_STAGE=install` | nothing was changed; the request is `failed` and Zeus holds |
  | after it, a timeout, or health never passing | the commit that was live before is put back by running *its own* script on it, once; if that fails too, the alert names both versions |
- **Be safe to run again** on the same commit and on an older one: the rollback is exactly that.
- **Never delete the previous version.** Set it aside; the rollback does not depend on it, but people do.
- **Use only what the commit carries and what the server already has.** No downloads of code to run, no commands from files Hermes delivered.

## What Zeus never does with it
Zeus never passes it arguments from a message, a page or a config, never runs a different script, and never runs it for a commit that is not on `main` or was not approved (a signed approval line in the log is checked first).
