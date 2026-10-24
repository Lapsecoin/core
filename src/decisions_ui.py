"""The Decisions page: what the node decided when it started, and why
(decisions.py). Private port only, like Settings: it names the node's address,
its folders and what it was given."""

from flask import render_template, request

import netcheck


def register(app, node, pool=None):
    @app.route("/decisions", endpoint="decisions")
    def decisions():
        recorded = getattr(node, "decisions", None)
        # The connection check takes a few seconds of real network traffic,
        # so it runs only when asked for, never on a plain page load.
        findings = None
        udp = getattr(getattr(node, "gossip", None), "udp", None)
        can_check = udp is not None and pool is not None
        if can_check and request.args.get("check"):
            findings = netcheck.judge(netcheck.gather(udp, pool))
        return render_template("decisions.html", title="Decisions",
                               groups=recorded.groups() if recorded is not None else [],
                               can_check=can_check, findings=findings)
