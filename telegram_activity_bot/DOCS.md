# Telegram Activity Bot - Add-on Docs

## Configuration options

Required:

- `telegram_bot_token` (`password`): Bot token from BotFather.
- `openai_api_key` (`password`): API key for OpenAI Responses.
- `openai_model` (`string`, default `gpt-5.2`): Responses model to use (for example `gpt-5.2` or `gpt-5.2-chat-latest`).

Optional access control:

- `allowed_chat_ids` (`string`): Comma-separated Telegram chat IDs allowed for tracking/replies/posts.
- `admin_user_ids` (`string`): Comma-separated Telegram user IDs reserved for future admin flows (for example `/teach`).

Bot identity:

- `bot_username` (`string`): Bot username (with or without `@`). Needed for robust mention detection.

Style file behavior:

- `style_post_filename` (`string`, default `style_post.md`): File loaded from `/config/{style_post_filename}` for ambient/eigenstaendige posts.
- `style_reply_filename` (`string`, default `style_reply.md`): File loaded from `/config/{style_reply_filename}` for mention/reply answers.
- `style_reload_seconds` (`int`, default `60`): Reload interval for style file.

Activity model:

- `activity_window_seconds` (`int`, default `300`): Sliding time window for counting messages.
- `activity_min_msgs_per_window` (`int`, default `3`): Minimum count before ambient posts can be considered.
- `ambient_enabled` (`bool`, default `false`): Enables ambient comments based on activity metrics only.
- `min_seconds_between_posts` (`int`, default `120`): Cooldown for ambient posts per chat only.
- `max_posts_per_day` (`int`, default `0`): Hard daily cap for ambient posts per chat only (`0` disables cap).

Mention behavior:

- `reply_on_mention` (`bool`, default `true`): Enables reply engine. Mention/reply triggers are immediate; otherwise a queue timer trigger is used.

## Privacy and storage

- Persistent state is stored at `/data/state.json`.
- Non-mentioned message content is never persisted.
- The bot stores numeric activity timestamps only for normal messages.
- Reply context queue is in-memory only (not persisted): up to 30 latest messages including sender usernames.
- Replies are generated from the full queue context. Immediate trigger on mention/reply; otherwise trigger when more than 2 minutes passed since first post-call queue element.
- Persistent memory file: `/config/memory.md` (max 5000 chars), included in every reply context; it always keeps the latest ambient post under `## Last Ambient Post`.

## OpenAI policy used

- Responses API with configurable `model` (`openai_model`, default `gpt-5.2`)
- `store=false`
- Short outputs (`max_output_tokens` kept small)
- Reply calls use `reasoning={"effort":"low"}` with `tools=[{"type":"web_search"}]`, `tool_choice="auto"`, and `include=["web_search_call.action.sources"]`
- Ambient and memory-update calls keep `reasoning={"effort":"low"}` but run without tools

## Style file locations

Provide your style note files in the add-on config directory. With `addon_config` mapping, they are mounted read/write inside the container as `/config`.

Default path inside container:

- `/config/style_post.md`
- `/config/style_reply.md`
- `/config/memory.md`
- `/config/nicks.md` (for ambient style randomization)

## Troubleshooting

"Bot never posts":

- Verify `allowed_chat_ids` includes the real group ID.
- Verify `bot_username` matches the Telegram bot username.
- Verify ambient cooldown/day-cap settings are not too strict.
- Verify `ambient_enabled=true` for ambient mode (default is off).

"Activity is not detected":

- Telegram privacy mode may still be enabled.
- Bot may not be group admin.
- In that case Telegram may only deliver mentions/replies/commands.

"Mention replies do not trigger":

- `reply_on_mention` may be `false`.
- `bot_username` may be empty or mismatched.
- Group may not deliver full message stream to bot due to privacy settings.

"Queue-timer replies do not trigger":

- The trigger needs at least 2 messages after the last API call.
- The second-or-later message must arrive more than 120 seconds after the trigger window start.
