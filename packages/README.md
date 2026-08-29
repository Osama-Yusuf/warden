# packages

The whole family tree. warden is a `uv` workspace, so these four packages live together and import each other without any `pip install` gymnastics.

```
core     the muscle: talks to every database, does the validating and quoting
  |
  +-- web       wraps core in an HTTP server + a single-page UI
  +-- cli       wraps core in a terminal tool
  +-- desktop   wraps web in a native window (it's just web wearing a coat)
```

**The rule of thumb:** anything that touches a database or needs to be *right* (SQL quoting, identifier checks, the audit trail) lives in `core`. The others are just different faces on top of it. If you're tempted to write engine logic in `web` or `cli`, stop, that belongs in `core`.

New here? Start in [`core/`](core/), then pick the face you care about.

Run everything from the repo root: `uv sync`, then `make test` (or poke around the [`tools/`](../tools/) scripts).
