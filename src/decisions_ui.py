"""The Decisions page: what the node decided when it started, and why
(decisions.py). Private port only, like Settings: it names the node's address,
its folders and what it was given."""

from flask import render_template


def register(app, node):
    @app.route("/decisions", endpoint="decisions")
    def decisions():
        recorded = getattr(node, "decisions", None)
        return render_template("decisions.html", title="Decisions",
                               groups=recorded.groups() if recorded is not None else [])
