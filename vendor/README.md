# vendor/

Third-party JS served straight from disk, the same way `lapsecoin.svg`
already is for the favicon, rather than fetched from a CDN at runtime: a
node's web UI should keep working with the outside internet unreachable.

- `force-graph.min.js` - [vasturiano/force-graph](https://github.com/vasturiano/force-graph),
  MIT licensed, v1.51.4, used unmodified. Pulled in for the `/network` page's
  peer graph instead of hand-rolling force-directed layout math.

- `markdown-toolbar-element.js` - [github/markdown-toolbar-element](https://github.com/github/markdown-toolbar-element),
  MIT licensed, v2.2.3, used unmodified (npm's `dist/index.js`, an ES
  module). The same toolbar-over-a-textarea custom element Remark42's own
  compose box uses (`<markdown-toolbar>` + `<md-bold>`/`<md-italic>`/etc.),
  pulled in for the `/board` compose box instead of hand-rolling
  select-wrap-restore-selection textarea logic.
