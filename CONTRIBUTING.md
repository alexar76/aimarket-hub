<!-- satellite-contributing -->
# Contributing to AIMarket Hub

Pull requests are welcome in [aimarket-hub](https://github.com/alexar76/aimarket-hub).
History here is append-only (no force-push), the same as
[metis](https://github.com/alexar76/metis) and [aicom](https://github.com/alexar76/aicom):
a merged pull request is imported back into the canonical monorepo and synced here
again, so your commit stays. Open an issue first for anything larger than a fix,
with the version, a reproduction and the expected result. Do not send Hub changes
to aicom. For protocol specification changes, use the separate
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
