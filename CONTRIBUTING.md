# Contributing to pig-agri

Thanks for your interest. pig-agri is research software maintained by a single author,
so contributions, bug reports, and questions are all welcome.

## Development setup

```bash
uv sync --extra dev          # install runtime + dev dependencies
docker compose up -d         # start PostgreSQL (schema: sql/init.sql)
uv run pytest                # run the test suite
uv run playwright install chromium   # only needed for the frontend checks below
```

## Verification layers

Three layers, and they do not overlap. Run whichever ones your change touches.

| Command | Covers | Blind to |
|---|---|---|
| `uv run pytest -p no:cacheprovider` | Everything server-side | The browser |
| `./scripts/check_js.sh` | ES module syntax in `static/js/` | Whether the module actually loads |
| `uv run python scripts/check_frontend_ux.py --url <app> --mode polished` | Real interaction in headless Chromium: menus, `<details>` accordions, help mode, keyboard handlers, console errors | Anything not asserted |

Two traps worth knowing before you save time by skipping a layer:

- **Do not run `node --check static/js/<file>.js`.** The `.js` extension makes node use
  the CommonJS parser, which accepts errors that break these ES modules outright. Use
  `check_js.sh`, which copies to `.mjs` first.
- **Syntax passing is not the module loading.** `import { X } from './y.js'` where `y.js`
  never exports `X` fails only at runtime. Grep the call sites when you rename an export.

`check_frontend_ux.py` needs a running app and is **read-only by contract**: it never
PUTs settings, saves masks, deletes recordings, or sends notifications, so it is safe to
point at a live deployment. Keep new assertions read-only too.

The MOT tracker is an external dependency (HybridSORT) cloned into `ref/HybridSORT/`;
see `docs/hybridsort-local-patches.md` for the local modifications. Tests that do not
need the tracker run without it.

## How we work

- **Spec-first.** Non-trivial changes start with a design spec and an implementation plan
  under `docs/superpowers/specs` and `docs/superpowers/plans`.
- **Test-backed.** New behavior comes with tests; keep the suite green.
- **Small commits** with clear messages.

## Submitting changes

1. Fork and create a feature branch.
2. Make your change with accompanying tests.
3. Run `uv run pytest` and make sure it passes. If you touched `static/`, also run
   `./scripts/check_js.sh` and `scripts/check_frontend_ux.py` against a running app.
4. Open a pull request describing the change and the motivation.

## Reporting issues

Open a GitHub issue with steps to reproduce, expected vs. actual behavior, and your
environment. For security-sensitive reports, please contact the maintainer privately.
