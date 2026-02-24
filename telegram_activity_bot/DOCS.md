# Telegram Activity Bot - Add-on Docs

## Configuration options

Required:

- `telegram_bot_token` (`password`): Bot token from BotFather.
- `openai_api_key` (`password`): API key for OpenAI Responses.
- `openai_model` (`string`, default `gpt-5.2`): Responses model to use (for example `gpt-5.2` or `gpt-5-mini`).

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
- `ambient_enabled` (`bool`, default `true`): Enables ambient comments based on activity metrics only.
- `min_seconds_between_posts` (`int`, default `600`): Cooldown between bot posts per chat.
- `max_posts_per_day` (`int`, default `0`): Hard daily post cap per chat (`0` disables cap).

Mention behavior:

- `reply_on_mention` (`bool`, default `true`): Enables mention/reply responses.

## Privacy and storage

- Persistent state is stored at `/data/state.json`.
- Non-mentioned message content is never persisted.
- The bot stores numeric activity timestamps only for normal messages.
- Mention/reply text is used in-memory to generate an immediate response.
- Mention context is truncated to 1000 chars and replied-to context to 500 chars.
- Reply context includes sender usernames/IDs for current and replied-to messages.
- Persistent memory file: `/config/memory.md` (max 2000 chars), included in every reply context.

## OpenAI policy used

- Responses API with configurable `model` (`openai_model`, default `gpt-5.2`)
- `store=false`
- Short outputs (`max_output_tokens` kept small)
- No tool use, no browsing integration

## Style file locations

Provide your style note files in the add-on config directory. With `addon_config` mapping, they are mounted read/write inside the container as `/config`.

Default path inside container:

- `/config/style_post.md`
- `/config/style_reply.md`
- `/config/memory.md`

## Troubleshooting

"Bot never posts":

- Verify `allowed_chat_ids` includes the real group ID.
- Verify `bot_username` matches the Telegram bot username.
- Verify cooldown/day-cap settings are not too strict.
- Verify `ambient_enabled=true` for ambient mode.

"Activity is not detected":

- Telegram privacy mode may still be enabled.
- Bot may not be group admin.
- In that case Telegram may only deliver mentions/replies/commands.

"Mention replies do not trigger":

- `reply_on_mention` may be `false`.
- `bot_username` may be empty or mismatched.
- Group may not deliver full message stream to bot due to privacy settings.
