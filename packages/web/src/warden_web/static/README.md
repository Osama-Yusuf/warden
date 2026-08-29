# static (the front end)

No build step. No npm. No webpack meltdown at 2am. It's HTML, CSS, and hand-written JS, served straight off disk. Edit a file, refresh the page, done.

- **`index.html`** the shell: the markup, then a `<link>` to the CSS and nine `<script>` tags. Used to be one 5,500-line monster, now it's a tidy ~180.
- **[`css/app.css`](css/)** all the styles. Theme-aware (light/dark), CSS variables up top.
- **[`js/`](js/)** the app logic, split into nine files by concern. Read [that folder's note](js/) before you go rearranging things, load order matters.

### The one gotcha
The scripts are **classic scripts, not ES modules**. They all share one global scope, which is exactly why an inline `onclick="viewQuery()"` in the HTML just works. If you convert them to modules, every one of those 130-ish inline handlers breaks. Don't, unless you're ready to rewire all of them.

Server side, `server.py`'s `do_GET` only serves `/css/*` and `/js/*` (path-traversal safe), so a new asset needs to land under one of those.
