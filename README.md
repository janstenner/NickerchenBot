# Telegram Activity Bot for Home Assistant OS

This repository contains a Home Assistant OS App (Add-on) that runs a Telegram bot with:

- Telegram Bot API long polling (`getUpdates` with persisted `offset`)
- OpenAI Responses API using configurable `openai_model` (default `gpt-5.2`) and `store=false`
- Strict storage minimization: only activity timestamps are persisted by default
- Queue-driven reply responses, plus optional ambient comments based on activity counts only

## What the bot does

- Tracks message activity in allowed group chats as timestamp-only metrics.
- Collects all incoming group messages in an in-memory queue (max 30) with usernames.
- Sends a reply immediately on `@mention` or reply-to-bot events.
- Otherwise sends a reply when queue timer conditions are met (>2 minutes since first post-call queue element, with at least 2 new messages).
- Optionally posts short ambient comments based on activity level, without using chat content.
- Reloads separate style/rule notes from `/config/style_post.md` and `/config/style_reply.md` (or configured filenames) at runtime.
- Maintains `/config/memory.md` (max 50000 chars) for persistent bot memory used in replies, including the latest ambient post.

## What the bot does not do

- It does not persist non-mentioned chat message text.
- Queue message text is kept in-memory only (not persisted to `/data/state.json`).
- It does not use local tools, shell execution, or files outside `/data` and `/config`.
- It does not log Telegram message text, prompt text, or API keys.

## Security and privacy model

- Persistent state file: `/data/state.json`
- Stored data per chat: activity timestamps, last bot post timestamp, optional day counter
- Message queue context is in-memory only (max 30 entries, includes usernames, not persisted)
- Queue context is hard-limited in size before OpenAI calls
- OpenAI responses are requested with `store=false`

## Telegram requirements (important)

To reliably measure group activity, Telegram must deliver group messages to the bot. In many groups this requires:

- Bot is group admin, and/or
- Bot privacy mode disabled (`/setprivacy` with BotFather)

If privacy mode remains enabled, Telegram may only deliver commands, mentions, and replies, so activity metrics can appear incomplete.

## Installation (Home Assistant OS)

1. In Home Assistant, add this GitHub repository URL as an Add-on repository.
2. Install `Telegram Activity Bot`.
3. Configure required options in the Add-on UI:
   - `telegram_bot_token`
   - `openai_api_key`
4. Set optional controls (`allowed_chat_ids`, activity thresholds, cooldown, etc.).
5. Create two style files in the add-on config folder (mapped read/write to `/config`):
   - ambient/eigenstaendige posts: `style_post.md`
   - replies auf mentions/replies: `style_reply.md`
6. Create `nicks.md` in the same folder (`/config`) for ambient random line injection.
7. Start the app.

See app-specific details in [`telegram_activity_bot/DOCS.md`](telegram_activity_bot/DOCS.md).
