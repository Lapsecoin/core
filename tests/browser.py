"""Posting a form the way a browser does: load the page, post what its form
holds, follow the redirect the answer is. The token a form carries works
once, so each post starts from a freshly loaded page."""

import re


def tokens(client, page):
    html = client.get(page).get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    form = re.search(r'name="form_token" value="([^"]+)"', html)
    return csrf, form.group(1) if form else ""


def submit(client, path, page=None, follow=True, **fields):
    """POST `fields` to path with the tokens of `page` (default: path)."""
    csrf, form = tokens(client, page or path)
    return client.post(path, data={"csrf_token": csrf, "form_token": form, **fields},
                       follow_redirects=follow)
