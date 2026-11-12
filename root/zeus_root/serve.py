"""Serve's mapping and the Tailscale operator, read before every run (design §16.4 step 6, private design 5.5 step 4).

The key page must still come from root's page folder and its `/api` from the gate, on the key page's own port and
nowhere else, never through Funnel; no Tailscale operator may be set (an operator can change Serve without root), and
tailscaled may not let another account fetch the server's certificate (`TS_PERMIT_CERT_UID`), which could serve a
look-alike page on the key page's exact origin. Each is a look at one moment: it catches a mapping that is still
changed when the run starts, not one that was changed and put back in between (private design 12.3a).

The JSON shapes are those of Tailscale's `ipn.ServeConfig` (`TCP`, `Web`, `AllowFunnel`) and of `tailscale debug
prefs` (`OperatorUser`), written from memory; the install's self-test (Z5) confirms them on {{SERVER}}. Anything that
does not read as expected is a refusal (fail closed).
"""
from . import canon

TAILSCALE = "/usr/bin/tailscale"
SYSTEMCTL = "/usr/bin/systemctl"
TIMEOUT_S = 15
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
    env = ctx.cmd([SYSTEMCTL, "show", "tailscaled.service", "-p", "Environment"], TIMEOUT_S)
    if env.code != 0:
        return "serve-unreadable"
    if "TS_PERMIT_CERT_UID" in env.out:
        return "cert-uid-set"
    return None
