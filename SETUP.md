# SETUP — build instructions for the AI agent

You are building the whole system from this repository. Work through the steps in order and verify each one
before the next. Ask the human only for the items marked HUMAN; collect them and ask in batches.

Rules for the whole job:

- Secrets live only in `.env` files on the NAS and in `~/.claude/settings.json` on the PC. Never print them,
  never commit them. When inspecting containers, select fields; do not dump the environment.
- Every `<placeholder>` in the copied files must be replaced. At the end, grep the deployed files for `<` + a
  placeholder name and confirm none is left.
- Text files must be LF. Pages are written through SilverBullet's `/.fs` API, not copied into the folder,
  unless the space is still empty.
- Paths below are Synology defaults (`/volume1/docker/…`). On Synology, `docker` is `/usr/local/bin/docker`
  and needs `sudo`; `tailscale` is `/var/packages/Tailscale/target/bin/tailscale`.

## 0. HUMAN prerequisites

1. Install **Container Manager** (DSM Package Center).
2. Enable **SSH** (Control Panel → Terminal & SNMP) and give you host, port, and an administrators-group login.
3. Install **Tailscale** (Package Center), sign in, and allow Funnel for the node in the Tailscale admin console.

Ask once, before starting:

- SilverBullet login name and password, and whether port 3001 is free.
- Which optional parts: `audio/` (needs an OpenRouter key and an Anthropic key), backup push to a private git
  remote (needs a repository and an access token), the ally endpoint.
- Whether claude.ai will be used (needs the Funnel in step 4).

Generate four independent secrets with `python -c "import secrets;print(secrets.token_urlsafe(32))"`:
`SB_AUTH_TOKEN`, `MCP_PATH_SECRET`, `SHARE_SECRET`, and (if used) `ALLY_SECRET`. With `audio/`, two more:
`AUDIO_PATH_SECRET`, `KB_PATH_SECRET`.

## 1. SilverBullet

1. `mkdir -p /volume1/docker/silverbullet/space`.
2. Copy `silverbullet/compose.yaml` there; fill `SB_USER` and `SB_AUTH_TOKEN`.
3. Start it (Container Manager → Project → create from the folder, or `docker compose up -d`).
4. Verify: `GET http://<NAS-IP>:3001/.fs` with headers `Authorization: Bearer <SB_AUTH_TOKEN>` and
   `X-Sync-Mode: true` returns a JSON list. Without the token it must not.
5. HUMAN: open `http://<NAS-IP>:3001`, log in with "Remember me" checked.

## 2. Space pages

Upload everything under `space/` to the same paths in the space (`PUT /.fs/<path>`, `Content-Type:
text/markdown`, same two headers).

- If the space already has an `index` page, do not overwrite it: merge the sections `## AI 守則`, `## 知識庫`,
  `## kb 音檔記錄`, `## 系統` into it. The heading `## kb 音檔記錄` must exist exactly; `audio/` writes under it.
- Fill placeholders: `<node>.<tailnet>.ts.net` and `<SHARE_SECRET>` in `Config/SharePage` and
  `Config/SystemStatus`; the two kb-audio links in `index` (remove them if `audio/` is not installed).
- `AI 守則/SilverBullet 維護手冊` must keep this exact path; the bridge's `readme()` reads it.
- Verify: list `/.fs` and compare sizes with the local files.
- HUMAN: run `System: Reload` in SilverBullet (Ctrl+/), confirm the share button and theme appear.

## 3. MCP bridge

1. Copy `bridge/` to `/volume1/docker/silverbullet-mcp/`; `mkdir -p history certs` inside it.
2. `cp .env.example .env` and fill: `SB_URL`, `SB_TOKEN` (= `SB_AUTH_TOKEN`), `MCP_PATH_SECRET`,
   `SHARE_SECRET`, optionally `ALLY_SECRET`. Leave the two `SSL_` lines empty when using Funnel.
3. Build and start: Container Manager project from the folder, or `docker compose up -d --build`.
4. Verify:
   - `docker logs silverbullet-mcp` shows the build line and `git history enabled`.
   - An MCP client connected to `http://<NAS-IP>:8008/<MCP_PATH_SECRET>/mcp` lists the tools; `readme()` returns
     the manual with a version line.
   - `http://<NAS-IP>:8008/wrong/mcp` → 404. `GET /status/<SHARE_SECRET>` → the status page.
   - `write_page` a test page, `page_history` shows the commit, `restore_page` works, then delete the page.

## 4. Public entry (needed for claude.ai)

1. `tailscale funnel --bg 8008`.
2. Verify from outside the LAN: `https://<node>.<tailnet>.ts.net/<MCP_PATH_SECRET>/mcp` answers;
   any other path is 404.
3. HUMAN: claude.ai → Settings → Connectors → Add custom connector, URL = that address. Give them the full URL
   to paste. OAuth fields stay empty.

## 5. Watchdog and daily backup

1. Edit the constants at the top of `funnel_watchdog.sh`: `HOST`, and `GITLAB_REPO` if backup push is used.
2. Backup push (optional): write the access token to `/volume1/docker/silverbullet-mcp/.gitlab-token`,
   `chmod 600`, owner root. Without the file the push step is skipped.
3. HUMAN: DSM Task Scheduler → new user-defined script, user **root**, every hour, command
   `bash /volume1/docker/silverbullet-mcp/funnel_watchdog_hourly.sh`. Run it once by hand.
4. Verify: `/tmp/funnel_watchdog_hb` is refreshed every minute; pressing the button on `Config/FunnelRestart`
   produces `Trigger/funnel-restart-result` within about a minute; after 04:00 the next day
   `Trigger/backup-status` exists and an archive is in `/volume1/docker/silverbullet-backups/`.

## 6. kb-audio (optional)

1. Copy `audio/` to `/volume1/docker/kb-audio/`; `mkdir -p jobs` there and `mkdir -p /volume1/kb-media`.
2. Fill `.env`: both path secrets, both API keys, `MCP_URL` (the LAN bridge URL with its secret).
3. `docker compose build`, then start the project.
4. `tailscale funnel --bg --https=8443 8009`.
5. Verify: the public entry page loads at `https://<node>.<tailnet>.ts.net:8443/<AUDIO_PATH_SECRET>/`; a wrong
   secret is 404; `POST …/<KB_PATH_SECRET>/api/source` with a small `.md` file returns a page name and that
   page is readable through `kb_read_original`.

## 7. Claude Code on the PC

1. `pip install fastmcp httpx requests paramiko`.
2. Copy `claude/skill/` to `~/.claude/skills/silverbullet/`.
3. Merge `claude/settings.json` into `~/.claude/settings.json`: add the `env` entries and the `SessionStart`
   hook next to whatever is already there. Fill the placeholders; the hook's script path is the absolute path of
   `claude/kb_session_start.py` in this clone. Do not replace the existing file.
4. Append the numbered rules in `claude/CLAUDE.md` to `~/.claude/CLAUDE.md`.
5. Register the bridge as an MCP server for Claude Code:
   `claude mcp add --transport http silverbullet http://<NAS-IP>:8008/<MCP_PATH_SECRET>/mcp`
   (or rely on the claude.ai connector from step 4).
6. Verify: `echo '{}' | python claude/kb_session_start.py` (with the env set) prints JSON whose
   `additionalContext` holds the `kb_index` text. HUMAN: open a new session and confirm the index is in context.

## 8. Knowledge base acceptance

Run these on the fresh install, in a test category, then delete everything under `kb/` through `/.fs` so the
real kb starts empty.

1. `kb_write` a page → `kb_index` lists it under recently added → `kb_read` returns the summary,
   `kb_read_original` the original.
2. Same name again → refused. Bad name, tag with a space, decision without reasoning → each refused with the reason.
3. Write a page that replaces it → `kb_read` on the old page is refused and names the new one, before any review.
4. Write a page that merges another → `kb_read_original` on the new page contains both originals.
5. Run `tools/review_move_all.py` (env `SILVERBULLET_MCP_URL`) → rows appear on the category page, recently
   added is empty, the weekly staging page is gone, the replaced page is listed in `kb/index/deprecated`.
6. Change one character of the manual page → a `kb_write` carrying the old version is refused; after `readme()`
   it succeeds.
7. `POST /<wrong secret>/internal/kb_create_index` → 404.

## 9. Hand-over

Tell the human:

- The connector URL, the share base URL and the status URL (they contain secrets; give them once, in full).
- The review routine: new pages wait in `kb/index/week/<ISO week>`; look at the proposed category and tags on
  each row, fix what is wrong, then run `python tools/review_move_all.py` (optionally
  `<proposed>=<final>` pairs to retarget a category).
- Rotating a secret = change the `.env` line, recreate the container, update whoever holds the old URL.
- Maintenance notes are in `bridge/README.md` and `audio/README.md`.
