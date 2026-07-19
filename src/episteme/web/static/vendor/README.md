# Vendored front-end renderers

Self-hosted copies (no CDN at read time — standalone principle). Loaded lazily:
`post.html` includes them only when the post actually contains a `chart` or
`diagram` section.

| File | Package | Version | Used for |
|---|---|---|---|
| vega.min.js | vega | 6.2.0 | `chart` sections (vega-lite runtime) |
| vega-lite.min.js | vega-lite | 6.4.3 | `chart` sections |
| vega-embed.min.js | vega-embed | 7.1.0 | `chart` sections (embedding + dark theme) |
| mermaid.min.js | mermaid | 11.16.0 | `diagram` sections |

Re-vendor / upgrade with:

```sh
cd src/episteme/web/static/vendor
curl -sL -o vega.min.js       https://cdn.jsdelivr.net/npm/vega@6/build/vega.min.js
curl -sL -o vega-lite.min.js  https://cdn.jsdelivr.net/npm/vega-lite@6/build/vega-lite.min.js
curl -sL -o vega-embed.min.js https://cdn.jsdelivr.net/npm/vega-embed@7/build/vega-embed.min.js
curl -sL -o mermaid.min.js    https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js
```
