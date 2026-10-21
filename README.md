# Zeus

Zeus is the throne of an Elaia/Argus home setup ([argus-ui](https://github.com/Celeiritas/argus-ui)): **deployments that only happen when the owner approves them.**

1. **The request.** [Hermes](https://github.com/Celeiritas/hermes) notices a new commit on `main` of an allowlisted repository and drops a small request message. Zeus checks for himself that the commit really is on `main`, and shows it on the Throne page: repository, version, commit, changelog lines, test status, files changed.
2. **The yes.** The owner holds *Approve & deploy* for three seconds from an approved tailnet device. [Janus](https://github.com/Celeiritas/janus) verifies who is asking and from which device (`tailscale whois`, never a self-declared name); Zeus checks the request is still fresh, unexpired and for the commit the page showed, and writes a signed line to his log.
3. **The deploy.** Zeus checks out exactly that commit in the server's clone and runs the repository's own `scripts/deploy.sh`, then waits for its health check. If the install fails or the service doesn't come up, he puts the previous approved commit back on his own. Rolling back by hand is one hold.

**Zeus never fetches from anywhere but the allowlisted repositories, never deploys a branch, never runs a command from a message, a page or a config, and never pushes anywhere.** The branch (`main`) and the script (`scripts/deploy.sh`) are fixed in code, so no config can widen him. Nothing of Zeus runs while idle: a systemd path unit wakes him when a request arrives; everything else is a one-shot started by the page or by ssh.

On the server Zeus is one file, a Python 3 zipapp with no dependencies (`bash scripts/build.sh` → `dist/zeus`). He calls `git`, `janus`, `hermes`, `systemd-run` and `tailscale`, which the server already has.

## Repository
`docs/design.md` is the design: decisions, the allowlist (`repos.json`), the request record and its states, approval (page, ssh, signing), the deploy and what happens when it fails, config deploys and quarantine releases, the Throne page and its accessibility rules, logs, and the build order. `zeus/` is the package, `plugin/` the Elaia page, `units/` the systemd units, `scripts/` build, install and the privacy check, `tests/` the tests (`bash tests/run.sh`, with a fake git remote, a fake `janus` and a fake `systemd-run`).

Public text uses placeholders (`{{SERVER}}`, `{{PC}}`, `{{PHONE}}`, `{{SSH}}`, `{{HOME_PC}}`, `{{HOME_SERVER}}`) and never a real device, user, path or host; `scripts/check-private.sh` enforces that before every push. MIT licence.
