"""docs/whitepaper.md as the HTML the whitepaper page shows.

Used by the node (api.py) and by scripts/build_whitepaper.py, which publishes
the same page to GitHub Pages, so the two cannot drift apart. Needs nothing
beyond the markdown package.
"""

import re

import markdown


def sections(html):
    """Wrap the flat markdown output as a header (title and byline) plus one
    <section> per <h2>, so the page can lay each section out as its own
    two-column block instead of one very long column."""
    parts = re.split(r"(?=<h2[ >])", html)
    out = ['<header class="wp-head">%s</header>' % parts[0]]
    for part in parts[1:]:
        end = part.find("</h2>") + len("</h2>")
        out.append('<section class="wp-sec">%s<div class="wp-body">%s</div></section>'
                   % (part[:end], part[end:]))
    return "".join(out)


def render(text):
    return sections(markdown.markdown(text, extensions=["fenced_code", "tables"]))
