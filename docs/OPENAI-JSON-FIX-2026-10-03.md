# OpenAI retrieval response fix, 2026-10-03

Applied `reliquary-openai-json-contract.patch` from the user's Downloads
archive to base commit `7682184`. The supplied implementation and 29 regression
tests were applied without changes.

## Reproduction and fix

Both the local endpoint and `https://reliquary.arda.tr/openai/mcp` successfully
initialized and advertised 13 tools, but search/fetch returned human-readable
text in `content[0].text`. JSON parsing failed even though `structuredContent`
contained valid retrieval data. Error results had the same mismatch, and the
tools did not advertise output schemas.

OpenAI search/fetch now mirror `structuredContent` as JSON text and advertise
success output schemas. Errors, including rate limits, retain their error flag
and use the same JSON representation. Claude responses and other tools keep
their existing format.

## Verification

- New regression tests against unchanged application code: **20 failed,
  9 passed**, reproducing the issue.
- Patched host suite: **495 passed**, no skips.
- Patched Docker image suite, Python 3.12: **495 passed**, no skips;
  `pip check` passed. The first container test invocation omitted the test's
  `pyproject.toml` mount; correcting that test setup resolved its sole failure.
- Python compilation, `git diff --check`, and `uv lock --check` passed.
- Official MCP Python SDK 1.29.0 exercised both local and public HTTPS endpoints:
  initialization, discovery, capabilities, real search/fetch, missing documents,
  and empty query/ID errors passed. OpenAI text parsed to the exact structured
  result; successful results validated against the advertised JSON schemas.
  Claude retained its readable text format and 18-tool surface.
- A session established before recreation still worked afterward. The fetched
  document text and leading OpenAI search IDs matched the baseline. Counts stayed
  at 4,891 memories and 3,162 catalog records. No corpus writes were performed.
- Deployment mounts were unchanged, and the running `server.py` hash matched
  the patched checkout.

The unchanged dependency lock has five audit findings: anyio 4.13.0
(`PYSEC-2026-4025`, `PYSEC-2026-4024`; fixed version 4.14.2) and urllib3 2.7.0
(`PYSEC-2026-4177`, `PYSEC-2026-4176`, `PYSEC-2026-4175`; fixed version 2.8.0).
Dependency upgrades are outside this response-format patch.

## Deployment and rollback

Built `reliquary:openai-json-fix-20261003` and updated the local Compose override
to select it, then ran `docker compose up -d --no-deps app`. Qdrant, Ollama,
corpus, blob, compiled-page and OAuth state mounts were preserved. No registry
publication was performed.

The previous image `reliquary:local-audit-20260906` remains available. To roll
back application code, restore
`/home/arda/reliquary-deploy/backups/openai-json-fix-20261003/docker-compose.override.yml.before`
to `/home/arda/reliquary-deploy/docker-compose.override.yml`, then run
`docker compose up -d --no-deps app` from that deployment directory. Do not
restore older OAuth state during a code rollback.

## Separate client configuration finding

At investigation time, `/home/arda/.codex/config.toml` configured Reliquary,
while the active `/home/arda/.codex-gmail-personal/config.toml` did not. This chat
therefore had no callable Reliquary tools. Live verification used authenticated
MCP requests and the official SDK; it does not establish that the desktop
profile has loaded the connector. The response patch alone does not add a
connection to a different profile.
