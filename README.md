# github-core-clone

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/selemene04/github-core-clone/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/selemene04/github-core-clone/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A GitHub-compatible git forge in a single Python file (standard library only): git smart HTTP clone/push, the GitHub REST v3 subset, a GraphQL v4 subset, and MCP tools, all served by one process from its own in-memory store.

## Run it

```sh
python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=2xsky44s&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>

---

# Selemene Forge

A standalone, GitHub-compatible git forge in a single Python file
(`main.py`, standard library only — no git binary, no packages, no database).

One process serves, from its own in-memory store:

- **Git smart HTTP** — clone and push (`git-upload-pack` / `git-receive-pack`),
  protocol **v2** (`ls-refs`, `fetch` with shallow/deepen support) and v0,
  packfile parsing and generation, fast-forward enforcement with per-ref
  `report-status`.
- **GitHub REST v3 subset** — repositories, contents (read/write/delete with
  sha-conflict detection), branches and git refs, commits, labels, issues and
  comments, pull requests with three-way squash merges and changed-file diffs.
- **GraphQL v4 subset** — the queries and mutations `gh` and
  github-mcp-server send: `viewer`, `repository`, `node(id:)`, `__type`
  introspection, issue/pull-request reads, `createIssue`, `closeIssue`,
  `createPullRequest`, `mergePullRequest`, `addComment`, cursor pagination,
  and batched operations.
- **MCP tools** — Streamable HTTP JSON-RPC with the handshake-era protocol:
  `create_repo`, `get_repo`, `create_issue`, `list_issues`,
  `create_pull_request`, `merge_pull_request`, all acting on the same store
  as the REST surface.

## Running

Trial harness mode (make.fun): set `MAKEFUN_ENV_URL` and `MAKEFUN_SHOP_TOKEN`;
the forge binds an ephemeral port, registers it with the env, serves until the
env reports done, and exits 0.

Standalone mode (local testing):

```sh
MAKEFUN_SHOP_TOKEN=test-token FORGE_PORT=8791 python3 main.py
# practice suite:
node --experimental-strip-types sdk/run.ts --url http://127.0.0.1:8791 --token test-token
```

## Design notes

- **Determinism** — timestamps come from a logical clock (a counter rooted at
  a fixed epoch) and ids from counters, so identical request sequences yield
  identical state.
- **Auth** — every surface requires `Authorization: Bearer <token>` where the
  token is the process's own `MAKEFUN_SHOP_TOKEN`.
- **Storage** — git objects live in memory as `(type, bytes)` keyed by their
  SHA-1; refs, issues, pulls and labels are plain dictionaries guarded by one
  lock. Nothing touches the filesystem.
