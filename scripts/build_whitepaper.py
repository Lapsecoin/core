#!/usr/bin/env python3
"""Render the whitepaper page that the Pages workflow publishes at
https://lapsecoin.github.io/core/whitepaper.html

    python scripts/build_whitepaper.py --out site

It is the node's own whitepaper page: the same markdown rendering
(src/whitepaper_render.py), the same templates (templates_html/whitepaper.html
on base.html) and the same stylesheets, so it looks as it does on a node. Only
the navigation differs (base.html's public_site branch), since the node's pages
do not exist on the website. Needs markdown and jinja2, nothing else.

Writes <out>/whitepaper.html, <out>/css/ (base.css, pages/whitepaper.css and
the fonts) and <out>/favicon.svg. Nothing here is committed to git.
"""

import argparse
import os
import re
import shutil
import sys
import time

import jinja2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import whitepaper_render  # noqa: E402

SITE = "https://lapsecoin.github.io"
PUBLIC = SITE + "/core"
TEMPLATES = os.path.join(ROOT, "templates_html")

CARD_TITLE = "LapseCoin whitepaper"
CARD_DESCRIPTION = ("Peer-to-peer electronic cash, secured by a Verifiable Delay "
                    "Function instead of proof-of-work mining.")

# A node serves its files from the root of its own address; here they sit
# under /core/, so root-absolute references have to move with them.
ROOT_ABSOLUTE = re.compile(r'(?:href|src)="/(?!core/)[^"]*"')


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def render_page():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES),
                             autoescape=jinja2.select_autoescape(["html"]))
    env.globals["server_now"] = lambda: int(time.time())
    page = env.get_template("whitepaper.html").render(
        title="Whitepaper",
        rendered=whitepaper_render.render(read("docs", "whitepaper.md")),
        og={"title": CARD_TITLE, "description": CARD_DESCRIPTION},
        og_image=SITE + "/logo-512.png",
        site_url=PUBLIC, page_url=PUBLIC + "/whitepaper.html",
        local_version=read("VERSION").strip(),
        public_site=True, nav_active="whitepaper")
    page = page.replace('"/css/', '"/core/css/').replace('"/favicon.svg"', '"/core/favicon.svg"')
    leftover = ROOT_ABSOLUTE.findall(page)
    if leftover:
        raise SystemExit("root-absolute references would break under /core/: "
                         + ", ".join(sorted(set(leftover))))
    return page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    out = ap.parse_args().out

    write(os.path.join(out, "whitepaper.html"), render_page())

    base_css = read("templates_html", "css", "base.css")
    if '"/css/fonts/' not in base_css:
        raise SystemExit("base.css no longer loads its fonts from /css/fonts/; update this script")
    # The corner logo is a mask of /favicon.svg, which sits one level up here.
    write(os.path.join(out, "css", "base.css"),
          base_css.replace('"/css/fonts/', '"fonts/').replace("url(/favicon.svg)", "url(../favicon.svg)"))
    write(os.path.join(out, "css", "pages", "whitepaper.css"),
          read("templates_html", "css", "pages", "whitepaper.css"))
    fonts = os.path.join(ROOT, "templates_html", "css", "fonts")
    os.makedirs(os.path.join(out, "css", "fonts"), exist_ok=True)
    for name in os.listdir(fonts):
        shutil.copyfile(os.path.join(fonts, name), os.path.join(out, "css", "fonts", name))
    shutil.copyfile(os.path.join(ROOT, "lapsecoin.svg"), os.path.join(out, "favicon.svg"))
    print("whitepaper:", PUBLIC + "/whitepaper.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
