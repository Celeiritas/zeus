# Zeus

Zeus is the throne of an Elaia/Argus home setup ([argus-ui](https://github.com/Celeiritas/argus-ui)): **deployments that only happen when the owner approves them.**

1. **The request.** [Hermes](https://github.com/Celeiritas/hermes) notices a new commit on `main` of an allowlisted repository and drops a small request message. Zeus checks for himself that the commit really is on `main`, and shows it on the Throne page: repository, version, commit, changelog lines, test status, files changed.
2. **The yes.** The owner holds *Approve & deploy* for three seconds from an approved tailnet device. [Janus](https://github.com/Celeiritas/janus) verifies who is asking and from which device (`tailscale whois`, never a self-declared name); Zeus checks the request is still fresh, unexpired and for the commit the page showed, and writes a signed line to his log.
3. **The deploy.** Zeus checks out exactly that commit in the server's clone and runs the repository's own `scripts/deploy.sh`, then waits for its health check. If the install fails or the service doesn't come up, he puts the previous approved commit back on his own. Rolling back by hand is one hold.

**Zeus never fetches from anywhere but the allowlisted repositories, never deploys a branch, never runs a command from a message, a page or a config, and never pushes anywhere.** The branch (`main`) and the script (`scripts/deploy.sh`) are fixed in code, so no config can widen him. Nothing of Zeus runs while idle: a systemd path unit wakes him when a request arrives; everything else is a one-shot started by the page or by ssh.

On the server Zeus is one file, a Python 3 zipapp with no dependencies (`bash scripts/build.sh` → `dist/zeus`). He calls `git`, `janus`, `hermes`, `systemd-run` and `tailscale`, which the server already has.

## Repository
`docs/design.md` is the design: decisions, the allowlist (`repos.json`), the request record and its states, approval (page, ssh, signing), the deploy and what happens when it fails, config deploys and quarantine releases, the Throne page and its accessibility rules, logs, the build order with what is built (§13), approval tiers and the decision inbox (§14), and staged packs (§15). `docs/contract.md` is what crosses each border (the request message, the record, `zeus status --json`, the signed log line, Janus's check, the command line and its exit codes) and `docs/deploy-script.md` what a repository's deploy script must honour. `zeus/` is the package, `plugin/` the Elaia page, `units/` the systemd units, `examples/` a sample `repos.json`, deploy script and pack install script (placeholders only), `scripts/` build, install and the privacy check, `tests/` the tests (`bash tests/run.sh`, with a fake git remote, a fake `janus` and a fake `systemd-run`; the Throne page in a browser with `bash tests/throne/run.sh <argus-ui checkout>`).

## Where it stands
Version 0.6.1 (2026-11-09). 0.6.0 is installed on `{{SERVER}}` and deploys argus-ui there; 0.6.1 corrects one printed line and these notes.

**Built** (milestones 0.1 to 0.6):
- **0.1 the queue:** `zeus queue`, `request`, `decline`, `status`.
- **0.2 the yes:** `approve` from the Throne or over ssh (`zeus deploy <repo>` is request + approve in one line), Janus's check, the signed log, `verify-log`.
- **0.3 the deploy:** `deploy-run` under `systemd-run`, hold vs auto-rollback, `rollback`, the alert file.
- **0.4 the Throne page:** the list, the 3-second hold, the live log, History with filters, the accessibility tests.
- **0.5 the decision inbox:** a session asks the owner a question with options (`zeus ask`), the owner answers with one tap under **Needs your answer** on the Throne (or `zeus answer`), and the session reads the answer back (`zeus asks --json`); the record and every answer are signed, an answer is given once and runs nothing.
- **0.6 staged packs** (`docs/design.md` §15): for a repository whose build needs files only `{{PC}}` has, the session builds there, stages an immutable pack on the server and asks with `zeus stage-request <repo> --version <v> --sha <commit>`; the Throne shows the version, commit, changelog, tests and pack size; on the owner's hold Zeus re-checks the commit on `main` and the pack's sha256, size and time, copies it, and runs the one fixed install script named in `repos.json` (`kind: staged-pack`, template `examples/install-pack.sh.example`); a rollback puts back the previous set-aside copy.

**Not built:**
- `approve-release` (#12): the command is a stub that says so, and there is no Release button.
- The Throne's inline diff of a commit's rules files: the badge names the files (`docs/design.md` §9, "As built").
- Approval tiers 1 and 2 (`docs/design.md` §14) are a proposal the owner has not confirmed; power actions (0.7) wait on that.

The build order with each milestone's status, and what is still open on the server (#13), is `docs/design.md` §13.

```
bash tests/run.sh                      # the tests (Python 3, bash, git; nothing is installed)
bash scripts/build.sh                  # -> dist/zeus, one file
./dist/zeus check-config --file examples/repos.example.json
bash scripts/install.sh                # on the server: ~/.local/bin/zeus + the systemd units
zeus init                              # once: ~/.config/zeus/secret.env (mode 600) and the data folders
```
Then copy your `repos.json` to `~/.config/zeus/` (the private config repo's deploy does this later) and run `zeus check-config`.

Public text uses placeholders (`{{SERVER}}`, `{{PC}}`, `{{PHONE}}`, `{{SSH}}`, `{{HOME_PC}}`, `{{HOME_SERVER}}`) and never a real device, user, path or host; `scripts/check-private.sh` enforces that before every push. MIT licence.
