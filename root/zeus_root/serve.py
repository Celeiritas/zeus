"""Serve's mapping and the Tailscale operator, read before every run (design §16.4 step 6, private design 5.5 step 4).

The key page must still come from root's page folder and its `/api` from the gate, on the key page's own port and
nowhere else, never through Funnel; no Tailscale operator may be set (an operator can change Serve without root), and
tailscaled may not let another account fetch the server's certificate (`TS_PERMIT_CERT_UID`), which could serve a
look-alike page on the key page's exact origin. Each is a look at one moment: it catches a mapping that is still
changed when the run starts, not one that was changed and put back in between (private design 12.3a).

TS_PERMIT_CERT_UID is read from tailscaled's environment as it runs (/proc/<MainPID>/environ), which holds what both
`Environment=` and `EnvironmentFile=` set: Tailscale's own unit reads /etc/default/tailscaled, the documented place for
it (review S2). Serve status keys other than the ones below, and any foreground session or service, are refused.

The JSON shapes are those of Tailscale's `ipn.ServeConfig` (`TCP`, `Web`, `AllowFunnel`) and of `tailscale debug
prefs` (`OperatorUser`), written from memory; the install's self-test (Z5) confirms them on {{SERVER}}. Anything that
does not read as expected is a refusal (fail closed).
"""
import re

from . import canon

TAILSCALE = "/usr/bin/tailscale"
SYSTEMCTL = "/usr/bin/systemctl"
TIMEOUT_S = 15
MAIN_PID = [SYSTEMCTL, "show", "tailscaled.service", "-p", "MainPID", "--value"]
ENVIRON_MAX = 1 << 20
STATUS_KEYS = {"TCP", "Web", "AllowFunnel", "Foreground", "Services"}
# reason -> root's one sentence for the key page; every one is class other with the attention kind serve-changed
REASONS = {
    "serve-changed": "Tailscale's settings for the key page changed, so Zeus refused. Nothing ran.",
    "operator-set": "A Tailscale operator is set on the server, so Zeus refused. Nothing ran.",
    "cert-uid-set": "Tailscale lets another account fetch the server's certificate, so Zeus refused. Nothing ran.",
    "serve-unreadable": "Zeus could not read Tailscale's settings for the key page, so it refused. Nothing ran.",
}


def _json(ctx, argv):
    res = ctx.cmd(argv, TIMEOUT_S)
    if res.code != 0:
        return None
    try:
        obj = canon.strict_loads(res.out.encode("utf-8"))
    except canon.Malformed:
        return None
    return obj if isinstance(obj, dict) else None


def _port_of(hostport):
    return hostport.rpartition(":")[2] if isinstance(hostport, str) else None


def mapping_problem(status, cfg):
    """None when `tailscale serve status --json` (as a dict) maps the key port exactly as installed, else a reason."""
    port, hostport = str(cfg.keyport), "%s:%d" % (cfg.rp_id, cfg.keyport)
    if set(status) - STATUS_KEYS:
        return "serve-changed"
    if status.get("Foreground") not in (None, {}) or status.get("Services") not in (None, {}):
        return "serve-changed"          # a foreground session or a service could map the key port too (review N4)
    tcp, web, funnel = status.get("TCP"), status.get("Web"), status.get("AllowFunnel", {})
    if not isinstance(tcp, dict) or not isinstance(web, dict) or not isinstance(funnel, (dict, type(None))):
        return "serve-changed"
    if tcp.get(port) != {"HTTPS": True}:
        return "serve-changed"
    if [k for k in web if _port_of(k) == port] != [hostport]:
        return "serve-changed"
    site = web[hostport]
    want = {"/": {"Path": cfg.page_dir}, "/api": {"Proxy": "http://127.0.0.1:%d" % cfg.gateport}}
    if not isinstance(site, dict) or site.get("Handlers") != want:
        return "serve-changed"
    if any(_port_of(k) == port and v is not False for k, v in (funnel or {}).items()):
        return "serve-changed"
    return None


def problem(ctx, cfg):
    """None when Serve's mapping is as installed, no operator is set and tailscaled has no TS_PERMIT_CERT_UID; else
    the reason (a key of REASONS)."""
    status = _json(ctx, [TAILSCALE, "serve", "status", "--json"])
    if status is None:
        return "serve-unreadable"
    reason = mapping_problem(status, cfg)
    if reason:
        return reason
    prefs = _json(ctx, [TAILSCALE, "debug", "prefs"])
    if prefs is None:
        return "serve-unreadable"
    if prefs.get("OperatorUser") not in (None, ""):
        return "operator-set"
    return cert_uid_problem(ctx)


def _read(path, limit):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return None


def cert_uid_problem(ctx):
    """None when tailscaled, as it runs, has no TS_PERMIT_CERT_UID in its environment; "cert-uid-set" when it has;
    "serve-unreadable" when that cannot be read: no main process, a pid whose name is not tailscaled (it restarted in
    between), an environment that does not read or is over 1 MB, or a MainPID that does not read the same again after
    the reads of /proc (tailscaled restarted and its pid was reused in between: re-check notes, item 5)."""
    res = ctx.cmd(MAIN_PID, TIMEOUT_S)
    pid = res.out.strip() if res.code == 0 else ""
    if not re.fullmatch(r"[1-9][0-9]{0,9}", pid):
        return "serve-unreadable"
    folder = ctx.proc / pid
    comm = _read(folder / "comm", 64)
    environ = _read(folder / "environ", ENVIRON_MAX + 1)
    if comm is None or comm.rstrip(b"\n") != b"tailscaled" or environ is None or len(environ) > ENVIRON_MAX:
        return "serve-unreadable"
    again = ctx.cmd(MAIN_PID, TIMEOUT_S)
    if again.code != 0 or again.out.strip() != pid:
        return "serve-unreadable"
    if any(var.startswith(b"TS_PERMIT_CERT_UID=") for var in environ.split(b"\0")):
        return "cert-uid-set"
    return None
