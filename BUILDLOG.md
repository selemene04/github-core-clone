# Build log — github-core-clone-01 entry

Kit: `~/workspace/makefun-work/github-core-clone-01-kit/github-core-clone-01-kit/`
Local runner: `node --experimental-strip-types sdk/run.ts --url http://127.0.0.1:8791 --token test-token [--perf]`
Server: `MAKEFUN_SHOP_TOKEN=test-token FORGE_PORT=8791 python3 main.py`

## Stage 0 — spec read (no code yet)
Read in full: RULES.txt, competition.yaml, requirements endpoint, starter,
sdk/run.ts, conformance manifest + wire scenario, git-client.ts (all 2145
lines), checks-git.ts (30), checks-rest.ts (repos/issues/pulls/protocol),
checks-graphql.ts (20), checks-mcp.ts (15), bench.ts.
Key facts: 120 checks (git 30 / repos 15 / issues 15 / pulls 20 / graphql 20 /
mcp 15 / protocol 5); token owner login is the scenario viewer `adaiversen`;
upload-pack must speak protocol v2; receive-pack is v0 with report-status;
MCP may use the legacy handshake era; four perf workloads (200 pushes, 50
concurrent depth-1 clones of a 200-commit repo, 2000 issue reads, 200 issue
writes).

## Stage 1 — full first implementation
Single-file `main.py` (stdlib only): in-memory git object store, pack
build/parse (incl. delta resolution), v0+v2 smart HTTP, REST repos/contents/
issues/pulls with 3-way squash merge, GraphQL parser+executor, MCP legacy-era
tools, harness lifecycle (register/done) + standalone dev mode.

**Run 1 (fresh server): 115/120** — git 27/30, repos 14/15, issues 15/15,
pulls 20/20, protocol 5/5, graphql 19/20, mcp 15/15.
Failures:
- git.fetch-shallow-{depth1,deepen,new-tip}: HTTP 400 on upload-pack.
- repos.commits: commit message read back with a trailing "\n" the check
  did not send.
- graphql.unknown-repository: response was an HTML error page.

## Stage 2 — three fixes
- Shallow fetch 400: off-by-one in the v2/v0 fetch parser — `"deepen "` is
  7 chars, sliced at 8, so `deepen 1` parsed as `int('')`. Fixed both parsers.
- Commit messages: `build_commit` appended a trailing newline; the REST
  commits view must echo the contents-API message verbatim. Now stored
  verbatim.
- HTML page: keep-alive corruption — the 401 for an unauthenticated GraphQL
  POST was answered **without reading the request body**, so the leftover
  bytes desynced the next request on the pooled connection and
  BaseHTTPRequestHandler emitted its HTML 400. The handler now always drains
  the request body before routing any response.

**Run 2 (fresh server): 120/120** — feature 600, agent 200.

## Stage 3 — performance (runner with --perf)
First perf run (server not restarted between runs — invalid for conformance,
but the workloads still measured): push p50 8,936 ms, clone 1,156 ms, read
**failed** (42.7 s for round 1, over the 60 s budget), write 4,295 ms.
Diagnosis: a fixed ~20–45 ms stall per request = Nagle/delayed-ACK on small
exchanges; plus `socketserver`'s default listen backlog of 5 dropping SYNs
under the clone workload's 50 simultaneous connects.

Fixes:
- `TCP_NODELAY` on every accepted socket.
- `request_queue_size = 512`, daemon threads, `block_on_close = False`.
- Depth-limited BFS in the fetch object computation; deque-based ancestry.

**Run 5 (fresh server, --perf): 120/120, perf 168/200, total 968/1000**
| workload | p50 (local) | T_ref (prod) | points |
|---|---|---|---|
| push (200) | 547 ms | 1,641 ms | 45 |
| clone (50 conc.) | 135 ms | 267 ms | 37 |
| read (2000) | 1,089 ms | 2,760 ms | 42 |
| write (200) | 125 ms | 349 ms | 44 |

Clone rounds were stable (135/126/135 ms) after the backlog fix. Local VM
numbers, not the production pod — but ahead of the reference forge's T_ref
on all four workloads here.

## Stage 4 — lifecycle verification
Against a mock env (register/done server on :8901): the forge registered its
ephemeral port, served requests during the trial window, polled /done, and
exited 0 when the env reported done (~8 s run). Standalone mode (no
MAKEFUN_ENV_URL) serves on $FORGE_PORT for the local runner.

## Final state
- Practice suite: **120/120 checks** (git 30, repos 15, issues 15, pulls 20,
  graphql 20, mcp 15, protocol 5), benchmark 168/200 → **968/1000** local.
- Source: `main.py` ≈ 104 KB, single file, stdlib only (caps: 16 MiB total /
  8 MiB per file — comfortably inside).
- Not pushed, not submitted (per task instructions).
