<!-- satellite-contributing -->
# Contributing to AIMarket Hub

This GitHub repository is a read-only mirror. Open an issue in
[aimarket-hub](https://github.com/alexar76/aimarket-hub/issues) with the version,
reproduction, expected result and a proposed patch. Direct PRs are not accepted
and are not imported into the canonical tree. Do not send Hub changes to aicom.
For protocol specification changes, use the separate
[protocol contribution process](https://github.com/alexar76/aimarket-protocol/blob/main/CONTRIBUTING.md).

## Local Checks

Python 3.12+ and uv:

```sh
git clone https://github.com/alexar76/aimarket-hub.git
cd aimarket-hub
uv sync --extra dev
uv run pytest tests/test_cli_quickstart_signing.py
```

Run the tests covering your changed module as well. Container builds from this
repository use `Dockerfile.standalone`, not the monorepo `Dockerfile`.
No Factory frontend, LLM account or production credential is required for these checks.
Follow SECURITY.md for security reports; never put credentials in an issue.
