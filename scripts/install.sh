#!/bin/bash
# scripts/install.sh - put Zeus on this machine (the server): ~/.local/bin/zeus from dist/zeus (built first when
# missing), the systemd user units from units/, and the Argus limits drop-in for zeus-queue.service.
#   bash scripts/install.sh            build if needed, install, enable zeus-queue.path
#   bash scripts/install.sh --no-units only the command (a PC, or a server without systemd)
# It never touches ~/.config/zeus (repos.json, secret.env, keys) or ~/.local/share/zeus: run `zeus init` once for the
# secret, and copy repos.json there yourself (or let the config repo's deploy do it).
set -e
R=$(cd "$(dirname "$0")/.." && pwd); cd "$R"
UNITS=1; [ "$1" = "--no-units" ] && UNITS=0
[ -f dist/zeus ] || bash scripts/build.sh
mkdir -p "$HOME/.local/bin"
install -m 755 dist/zeus "$HOME/.local/bin/zeus"
echo "installed ~/.local/bin/zeus ($("$HOME/.local/bin/zeus" --version))"
if [ $UNITS = 1 ]; then
  U="$HOME/.config/systemd/user"; mkdir -p "$U/zeus-queue.service.d"
  install -m 644 units/zeus-queue.path units/zeus-queue.service "$U/"
  install -m 644 units/argus-limits.conf "$U/zeus-queue.service.d/argus-limits.conf"
  systemctl --user daemon-reload
  systemctl --user enable --now zeus-queue.path
  echo "installed zeus-queue.path + zeus-queue.service (with argus-limits.conf); zeus-queue.path is enabled"
  A="${ARGUS_REGISTRY:-$HOME/.config/argus-ui/agents}"; mkdir -p "$A"   # the task-card notes Argus reads
  install -m 644 units/agents/zeus-queue.json units/agents/zeus-deploy.json "$A/"
  echo "installed the Argus cards for zeus-queue and zeus-deploy in $A"
fi
[ -f "$HOME/.config/zeus/secret.env" ] || echo "next: zeus init (makes ~/.config/zeus/secret.env), then copy repos.json to ~/.config/zeus/"
