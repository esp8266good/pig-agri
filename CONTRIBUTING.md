# Contributing to pig-agri

Thanks for your interest. pig-agri is research software maintained by a single author,
so contributions, bug reports, and questions are all welcome.

## Development setup

```bash
uv sync --extra dev          # install runtime + dev dependencies
docker compose up -d         # start PostgreSQL (schema: sql/init.sql)
uv run pytest                # run the test suite
```

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
3. Run `uv run pytest` and make sure it passes.
4. Open a pull request describing the change and the motivation.

## Reporting issues

Open a GitHub issue with steps to reproduce, expected vs. actual behavior, and your
environment. For security-sensitive reports, please contact the maintainer privately.
