# tag2now-save-admin

Admin tools for Tekken Tag Tournament 2 saves on the tag2now RPCN server: read
and edit the TUS save (`NPWR02973_00`, slot 1) that holds each player's ranks.

They run on the RPCN host, read RPCN's database read-only, and edit the save
file it points at. Every write takes a backup first, appends to an audit log,
and is verified. The save format is in [docs/](docs/TTT2%20TDT%20세이브%20포맷.md).

| Script | What it is |
|--------|------------|
| `script/tdt_admin.py` | The tool: `show`, `backup`, `restore`, `set-rank`, `floor`, `log`, `gc`, ... (`--help`) |
| `script/tdt_admin_server.py` | HTTP API over part of it, for the admin page in tag2now-BE |
| `script/floor_new_users.py` | Starts accounts created after the 2026-09-22 floor pass at the floor |
| `script/extract_tdt_ranks.py`, `script/find_rank_tdt.py` | Research helpers for the format |

Python 3.9+, standard library only.

## Tests

On Linux (writes go through `/tmp` and take `flock` locks), from `script/`:

```bash
python3 -m unittest discover -s tests -t .
```

From Windows, the same in a container:

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/w:ro" -w /w/script -e PYTHONDONTWRITEBYTECODE=1 python:3.9-slim python -m unittest discover -s tests -t .
```

## tdt_admin_server.py

tag2now-BE forwards an admin's request here. It is never exposed to the
internet.

```
FE ──▶ tag2now-BE (signed-in admin) ──X-API-Key + admin id/pw──▶ tdt_admin_server ──▶ save files
                                                                        │
                                              admin check, online check ▼
                                                                RPCN API server (31315)
```

- **Two checks per request.** `X-API-Key` must equal `TDT_ADMIN_API_KEY`, and the
  body's `admin_username` / `admin_password` must pass RPCN's
  `/admin/users/info` — an admin that is not banned. The password is the
  RPCS3-derived value tag2now-BE already sends to RPCN; it is never stored.
- **Writes are two steps.** Send the request with `dry_run: true` to get the
  changes and the save's `sha256`, then send it again with
  `expect_sha256` set to that value. A save that changed in between, or an
  account that is online, is refused (409) and nothing is written.
- **Not served:** `--force`, `floor --all`, `floor --redo`, `gc`, and anything
  that takes a file path. Those stay on the command line.

| Route | Body |
|-------|------|
| `POST /saves/show` | `username`, `all_chars?` |
| `POST /saves/backups` | `username` |
| `POST /saves/log` | `username?`, `n?` (1..500, default 50) |
| `POST /saves/set-rank` | `username`, `char` (id, name or `all`), `rank` (code or name), `points?` |
| `POST /saves/set-account-rank` | `username`, `rank` |
| `POST /saves/floor` | `username`, `rank?`, `fix_points?`, `refloor?` |
| `POST /saves/restore` | `username`, `label` |

Every body also carries `admin_username` and `admin_password`; the last four
also take `dry_run` and `expect_sha256`. A write answers
`{username, sha256, online, changes, applied, result}`, where `changes` lists
each character whose rank, points or streak differs.

Errors are `{"error": "<code>", "message": "..."}`:

| Status | Codes |
|--------|-------|
| 400 | `invalid_request`, `ambiguous_user` |
| 401 | `invalid_credentials` — wrong admin password |
| 403 | `invalid_api_key` — wrong `X-API-Key`; `forbidden` — not an active admin |
| 404 | `not_found`, `user_not_found`, `save_not_found`, `backup_not_found` |
| 409 | `online`, `save_changed`, `likely_demoted` (pass `refloor`) |
| 502 | `rpcn_unavailable` — the admin check could not reach RPCN |
| 503 | `online_unknown` — RPCN cannot say whether the account is online |
| 500 | anything else; the message says to see the server log |

Web writes show in the audit log with the admin as `user` and `via: "web"`.

### Deployment

The server ships as a Docker image and runs as the `save-admin` service of the
production compose stack, next to `be`. That file, `compose.prod.yml`, is owned
by tag2now-BE; this repository only releases the image.

- **No published port.** `be` calls `http://save-admin:8000` over the compose
  network, so nothing outside the instance can reach it.
- **RPCN on the host.** RPCN runs with `network_mode: host`, so the container
  reaches its API server as `http://host.docker.internal:31315` (`RPCN_API_URL`).
- **Same paths as the host.** The RPCN database, the save directory and the
  backup directory are mounted at their host paths. The host CLI can then use
  whatever this server wrote — `floor --redo` reopens backup paths straight
  from the audit log — and a `flock` taken here also holds against the CLI.

| Variable | Value |
|----------|-------|
| `TDT_ADMIN_API_KEY` | What `be` sends as `X-API-Key`. The server refuses to start without it |
| `RPCN_STAT_API_KEY` | rpcn.cfg `ApiServerApiKey` |
| `RPCN_API_URL` | `http://host.docker.internal:31315` |
| `TDT_ADMIN_BIND` | `0.0.0.0:8000` in the image; leave it |

**Release:** push a `v*` tag. `deploy.yml` runs the tests, pushes the image to
ECR, writes `SAVE_ADMIN_IMAGE_TAG` into the instance's `.env.prod`, and
restarts only `save-admin`. It uses the `tag2now` org's SSH and AWS settings,
like tag2now-BE and tag2now-FE, plus this repository's `ECR_REPOSITORY`
variable (`tag2now/save-admin`).
