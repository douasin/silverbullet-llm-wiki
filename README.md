# silverbullet-llm-wiki

**English** | [繁體中文](README.zh-TW.md)

A Karpathy-style [LLM wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) on
[SilverBullet](https://silverbullet.md): a self-hosted knowledge base that AI sessions read and write over MCP,
shared across every session you open.

This repository is a starter kit. You install three things on a Synology NAS by hand, then hand
[SETUP.md](SETUP.md) to an AI coding agent (Claude Code or similar) and it builds the rest.

## What you get

- **A knowledge base the AI maintains** (`kb/` in your note space). Sessions write facts, decisions with their
  reasons, and records of what happened; every later session starts with the index already in context and looks
  things up before answering from its own knowledge.
- **An MCP bridge** in front of SilverBullet: page tools, kb tools, per-page git history with restore, read-only
  share links, a status page.
- **Usage text that cannot go stale.** Tool usage lives in one page of the space, served live by a `readme()`
  tool with a version; writes carrying an outdated version are refused, so long-running sessions re-read it.
- **Daily backup and a watchdog** for the public entry (Tailscale Funnel).
- **The Claude Code side**: a SessionStart hook that loads the kb index, the rules block for `CLAUDE.md`,
  and a CLI skill.
- **Optional audio intake** (`audio/`): drop a recording, get a transcript and summary, import it into the kb.

## How it maps to the LLM wiki pattern

| LLM wiki | Here |
|---|---|
| Raw sources the LLM never modifies | `_source` pages and the original text below each page's separator |
| The wiki the LLM writes | `kb/<name>` pages: a one-line summary per category, plus reasons for decisions |
| The schema | One manual page, served by `readme()` |
| `index.md` | `kb/index` and one index page per category level, routed by tags |
| `log.md` | `kb/log` |
| Ingest | `kb_write`, one page at a time, after you approve category and tags |
| Query | `kb_index` → category pages → `kb_read` → `kb_read_original` only when the summary is not enough |
| Keeping pages current | A new page replaces, absorbs or retires an old one; the old one stops being readable and points to the new one |

Two things differ from the gist. Summaries and originals are read separately, so the AI pays for the original
only when it needs it. Integrity (index rows, backlinks, retired pages) is enforced by the tools at write time,
so there is no separate lint pass.

## Layout

| Path | What | Goes to |
|---|---|---|
| `silverbullet/` | SilverBullet compose file | NAS `/volume1/docker/silverbullet/` |
| `bridge/` | MCP bridge container, watchdog and backup scripts | NAS `/volume1/docker/silverbullet-mcp/` |
| `audio/` | Optional audio → transcript → kb container | NAS `/volume1/docker/kb-audio/` |
| `space/` | Pages the system needs: manual, home page skeleton, `Config/` | the SilverBullet space |
| `claude/` | Hook, `settings.json` block, `CLAUDE.md` block, CLI skill | your PC, under `~/.claude/` |
| `tools/` | Review script that files new pages into categories | your PC |

## Requirements

- A Synology NAS with Container Manager, SSH enabled, and Tailscale. Other Docker hosts work if you adjust paths.
- An AI coding agent with shell access on your PC, Python 3.11+.
- For claude.ai (web or phone): Tailscale Funnel, because its connectors only reach public HTTPS on port 443.
- For `audio/`: an OpenRouter API key (transcription) and an Anthropic API key (summaries).

## Getting started

1. Install Container Manager, enable SSH, install Tailscale and sign in.
2. Clone this repository on your PC.
3. Tell your AI agent: "Read SETUP.md and build it." It will ask you for the few things only you can do.

## Language

The manual page, tool replies and rejection messages are written in Traditional Chinese. The tools work in any
language; translate `space/AI 守則/SilverBullet 維護手冊.md` if you want the AI-facing text in another one.

## License

[MIT](LICENSE). One page is third-party: the code in `space/Config/MobileTapShield.md` comes from a
[SilverBullet community forum thread](https://community.silverbullet.md/t/fixing-workaround-the-silverbullet-codemirror-mobile-keyboard-drama-disable-accidental-taps/3709)
and belongs to its author.
