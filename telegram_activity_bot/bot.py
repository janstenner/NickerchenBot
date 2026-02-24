#!/usr/bin/env python3
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from openai import OpenAI


OPTIONS_PATH = "/data/options.json"
STATE_PATH = "/data/state.json"
CONFIG_DIR = "/config"
POLL_TIMEOUT_SECONDS = 25
AMBIENT_TICK_SECONDS = 10
MAX_ACTIVITY_ENTRIES_PER_CHAT = 10000
MAX_STYLE_CHARS = 20000
MAX_MENTION_CONTEXT_CHARS = 1000
MAX_REPLY_CONTEXT_CHARS = 500


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def summarize_exception(exc: Exception) -> str:
    name = exc.__class__.__name__
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        resp = getattr(exc, "response", None)
        if resp is not None:
            status_code = getattr(resp, "status_code", None)
    if isinstance(status_code, int):
        return f"{name}(status={status_code})"
    return name


def parse_csv_ints(value: str) -> Set[int]:
    out: Set[int] = set()
    if not value:
        return out
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            out.add(int(token))
        except ValueError:
            logging.warning("Ignoring non-integer ID in CSV option")
    return out


def parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
    return default


def normalize_bot_username(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if not v.startswith("@"):
        v = f"@{v}"
    return v.lower()


def load_options() -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "telegram_bot_token": "",
        "openai_api_key": "",
        "openai_model": "gpt-5-mini",
        "allowed_chat_ids": "",
        "admin_user_ids": "",
        "bot_username": "",
        "style_post_filename": "style_post.md",
        "style_reply_filename": "style_reply.md",
        "style_reload_seconds": 60,
        "activity_window_seconds": 300,
        "activity_min_msgs_per_window": 3,
        "ambient_enabled": True,
        "min_seconds_between_posts": 600,
        "max_posts_per_day": 0,
        "reply_on_mention": True,
    }

    data: Dict[str, Any] = {}
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
    except FileNotFoundError:
        logging.warning("Options file missing at /data/options.json")
    except Exception as exc:
        logging.error("Failed loading options file: %s", summarize_exception(exc))

    merged = {**defaults, **data}
    merged["allowed_chat_ids"] = parse_csv_ints(str(merged.get("allowed_chat_ids", "")))
    merged["admin_user_ids"] = parse_csv_ints(str(merged.get("admin_user_ids", "")))
    merged["bot_username"] = normalize_bot_username(str(merged.get("bot_username", "")))
    merged["openai_model"] = str(merged.get("openai_model", "gpt-5-mini")).strip() or "gpt-5-mini"
    legacy_style_filename = os.path.basename(str(merged.get("style_filename", "")) or "")
    default_post_style = legacy_style_filename or "style_post.md"
    default_reply_style = legacy_style_filename or "style_reply.md"
    merged["style_post_filename"] = os.path.basename(
        str(merged.get("style_post_filename", default_post_style)) or default_post_style
    )
    merged["style_reply_filename"] = os.path.basename(
        str(merged.get("style_reply_filename", default_reply_style)) or default_reply_style
    )

    merged["style_reload_seconds"] = max(5, int(merged.get("style_reload_seconds", 60)))
    merged["activity_window_seconds"] = max(10, int(merged.get("activity_window_seconds", 300)))
    merged["activity_min_msgs_per_window"] = max(1, int(merged.get("activity_min_msgs_per_window", 3)))
    merged["ambient_enabled"] = parse_bool(merged.get("ambient_enabled", True), True)
    merged["min_seconds_between_posts"] = max(0, int(merged.get("min_seconds_between_posts", 600)))
    merged["max_posts_per_day"] = max(0, int(merged.get("max_posts_per_day", 0)))
    merged["reply_on_mention"] = parse_bool(merged.get("reply_on_mention", True), True)

    if not merged["telegram_bot_token"]:
        raise RuntimeError("Missing required option: telegram_bot_token")
    if not merged["openai_api_key"]:
        raise RuntimeError("Missing required option: openai_api_key")

    return merged


def default_chat_state() -> Dict[str, Any]:
    return {
        "activity_timestamps": [],
        "last_post_ts": 0,
        "daily_count": 0,
        "daily_date": "",
    }


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError("state not object")
    except FileNotFoundError:
        state = {}
    except Exception as exc:
        logging.error("State load failed, creating new state: %s", summarize_exception(exc))
        state = {}

    if "telegram_offset" not in state or not isinstance(state.get("telegram_offset"), int):
        state["telegram_offset"] = 0
    if "chats" not in state or not isinstance(state.get("chats"), dict):
        state["chats"] = {}

    return state


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs("/data", exist_ok=True)
    tmp_path = f"{STATE_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp_path, STATE_PATH)


def mask_chat_id(chat_id: int) -> str:
    s = str(chat_id)
    if len(s) <= 6:
        return s
    return f"{s[:3]}...{s[-3:]}"


def get_chat_state(state: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
    chats: Dict[str, Any] = state["chats"]
    key = str(chat_id)
    if key not in chats or not isinstance(chats[key], dict):
        chats[key] = default_chat_state()
    chat = chats[key]
    if "activity_timestamps" not in chat or not isinstance(chat.get("activity_timestamps"), list):
        chat["activity_timestamps"] = []
    if "last_post_ts" not in chat:
        chat["last_post_ts"] = 0
    if "daily_count" not in chat:
        chat["daily_count"] = 0
    if "daily_date" not in chat:
        chat["daily_date"] = ""
    return chat


def prune_activity(chat_state: Dict[str, Any], now_ts: int, window_seconds: int) -> None:
    cutoff = now_ts - window_seconds
    kept = [int(ts) for ts in chat_state["activity_timestamps"] if isinstance(ts, (int, float)) and int(ts) >= cutoff]
    if len(kept) > MAX_ACTIVITY_ENTRIES_PER_CHAT:
        kept = kept[-MAX_ACTIVITY_ENTRIES_PER_CHAT:]
    chat_state["activity_timestamps"] = kept


def record_activity(state: Dict[str, Any], chat_id: int, now_ts: int, window_seconds: int) -> None:
    chat_state = get_chat_state(state, chat_id)
    chat_state["activity_timestamps"].append(now_ts)
    prune_activity(chat_state, now_ts, window_seconds)


def activity_metrics(chat_state: Dict[str, Any], window_seconds: int) -> Tuple[int, float]:
    count = len(chat_state["activity_timestamps"])
    per_min = (count * 60.0) / float(window_seconds)
    return count, per_min


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def can_post_now(chat_state: Dict[str, Any], now_ts: int, min_seconds_between_posts: int, max_posts_per_day: int) -> bool:
    last_post_ts = int(chat_state.get("last_post_ts", 0) or 0)
    if now_ts - last_post_ts < min_seconds_between_posts:
        return False

    if max_posts_per_day > 0:
        d = today_utc()
        if chat_state.get("daily_date") != d:
            chat_state["daily_date"] = d
            chat_state["daily_count"] = 0
        if int(chat_state.get("daily_count", 0) or 0) >= max_posts_per_day:
            return False

    return True


def can_post_reply(chat_state: Dict[str, Any], max_posts_per_day: int) -> bool:
    if max_posts_per_day <= 0:
        return True

    d = today_utc()
    if chat_state.get("daily_date") != d:
        chat_state["daily_date"] = d
        chat_state["daily_count"] = 0

    return int(chat_state.get("daily_count", 0) or 0) < max_posts_per_day


def post_block_reason(
    chat_state: Dict[str, Any],
    now_ts: int,
    min_seconds_between_posts: int,
    max_posts_per_day: int,
) -> str:
    last_post_ts = int(chat_state.get("last_post_ts", 0) or 0)
    since_last = now_ts - last_post_ts
    if since_last < min_seconds_between_posts:
        remaining = min_seconds_between_posts - since_last
        return f"cooldown({remaining}s)"

    if max_posts_per_day > 0:
        d = today_utc()
        if chat_state.get("daily_date") == d:
            daily_count = int(chat_state.get("daily_count", 0) or 0)
            if daily_count >= max_posts_per_day:
                return "daily_cap"

    return ""


def reply_block_reason(chat_state: Dict[str, Any], max_posts_per_day: int) -> str:
    if max_posts_per_day <= 0:
        return ""

    d = today_utc()
    if chat_state.get("daily_date") == d:
        daily_count = int(chat_state.get("daily_count", 0) or 0)
        if daily_count >= max_posts_per_day:
            return "daily_cap"

    return ""


def register_post(chat_state: Dict[str, Any], now_ts: int) -> None:
    chat_state["last_post_ts"] = now_ts
    d = today_utc()
    if chat_state.get("daily_date") != d:
        chat_state["daily_date"] = d
        chat_state["daily_count"] = 0
    chat_state["daily_count"] = int(chat_state.get("daily_count", 0) or 0) + 1


def compute_ambient_probability(msgs_per_min: float) -> float:
    # Probability per ambient tick (default tick ~10s)
    return min(0.6, 0.03 + (msgs_per_min / 100.0))


def is_group_message(message: Dict[str, Any]) -> bool:
    chat = message.get("chat") or {}
    return chat.get("type") in {"group", "supergroup"}


def is_allowed_chat(chat_id: int, allowed_chat_ids: Set[int]) -> bool:
    if not allowed_chat_ids:
        return True
    return chat_id in allowed_chat_ids


def message_text(message: Dict[str, Any]) -> str:
    text = message.get("text")
    if isinstance(text, str):
        return text
    caption = message.get("caption")
    if isinstance(caption, str):
        return caption
    return ""


def clamp_text(value: str, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) <= max_chars:
        return value
    return value[:max_chars]


def is_mention(text: str, bot_username: str) -> bool:
    if not bot_username:
        return False
    return bot_username in text.lower()


def is_reply_to_bot(message: Dict[str, Any], bot_username: str) -> bool:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return False

    frm = reply.get("from") or {}
    if not frm.get("is_bot", False):
        return False

    if not bot_username:
        return True

    reply_username = frm.get("username")
    if not isinstance(reply_username, str) or not reply_username:
        return False

    return normalize_bot_username(reply_username) == bot_username


class StyleCache:
    def __init__(self, filename: str, reload_seconds: int):
        self.filename = filename
        self.reload_seconds = reload_seconds
        self._last_load = 0.0
        self._cached = ""

    def get(self) -> str:
        now = time.time()
        if now - self._last_load < self.reload_seconds:
            return self._cached

        path = os.path.join(CONFIG_DIR, self.filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read(MAX_STYLE_CHARS)
                self._cached = content
        except FileNotFoundError:
            logging.warning("Style file missing at /config/%s", self.filename)
            self._cached = ""
        except Exception as exc:
            logging.error("Failed reading style file: %s", summarize_exception(exc))

        self._last_load = now
        return self._cached


def get_field(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def extract_text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        candidate = value.get("value") or value.get("text")
        return candidate if isinstance(candidate, str) else ""
    candidate = getattr(value, "value", None)
    if isinstance(candidate, str):
        return candidate
    candidate = getattr(value, "text", None)
    if isinstance(candidate, str):
        return candidate
    return ""


def extract_response_text(response: Any) -> str:
    output_text = get_field(response, "output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = get_field(response, "output")
    if isinstance(output, list):
        parts: List[str] = []
        for item in output:
            content = get_field(item, "content")
            if not isinstance(content, list):
                continue
            for block in content:
                text_val = extract_text_value(get_field(block, "text"))
                if text_val:
                    parts.append(text_val)
        if parts:
            return "\n".join(parts).strip()

    return ""


def response_debug_meta(response: Any) -> str:
    status = get_field(response, "status")
    status_str = status if isinstance(status, str) and status else "unknown"

    output = get_field(response, "output")
    output_count = len(output) if isinstance(output, list) else 0

    incomplete = get_field(response, "incomplete_details")
    reason = ""
    if incomplete is not None:
        reason_val = get_field(incomplete, "reason")
        if isinstance(reason_val, str):
            reason = reason_val

    if reason:
        return f"status={status_str} output_items={output_count} reason={reason}"
    return f"status={status_str} output_items={output_count}"


def create_openai_reply(
    client: OpenAI,
    model: str,
    style_text: str,
    mention_text: str,
    reply_text: str,
) -> Tuple[str, str]:
    mention_text = clamp_text(mention_text, MAX_MENTION_CONTEXT_CHARS)
    reply_text = clamp_text(reply_text, MAX_REPLY_CONTEXT_CHARS)

    prompt = (
        "You are a concise Telegram group assistant. "
        "Follow the style notes exactly. "
        "Keep output short and safe."
        "\n\nStyle notes:\n"
        f"{style_text or '(none)'}"
        "\n\nTask: Reply to the user message."
        "\nUser message:\n"
        f"{mention_text or '(empty)'}"
    )

    if reply_text:
        prompt += "\n\nReplied-to message:\n" + reply_text

    response = client.responses.create(
        model=model,
        store=False,
        input=prompt,
        max_output_tokens=140,
    )
    return extract_response_text(response), response_debug_meta(response)


def create_openai_ambient(client: OpenAI, model: str, style_text: str, count: int, msgs_per_min: float) -> Tuple[str, str]:
    prompt = (
        "You are a concise Telegram group assistant. "
        "Generate exactly one harmless sentence with no assumptions about specific chat content."
        "\n\nStyle notes:\n"
        f"{style_text or '(none)'}"
        "\n\nActivity metrics only:"
        f"\ncount_in_window={count}"
        f"\nmsgs_per_min={msgs_per_min:.2f}"
        "\n\nTask: Produce one short ambient comment."
    )

    response = client.responses.create(
        model=model,
        store=False,
        input=prompt,
        max_output_tokens=60,
    )
    return extract_response_text(response), response_debug_meta(response)


def telegram_get_updates(token: str, offset: int, timeout: int) -> List[Dict[str, Any]]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    resp = requests.get(
        url,
        params={"offset": offset, "timeout": timeout},
        timeout=timeout + 5,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Telegram getUpdates HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram getUpdates returned not ok")
    result = data.get("result")
    if isinstance(result, list):
        return result
    return []


def telegram_send_message(token: str, chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
    if not text.strip():
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True

    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"Telegram sendMessage HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram sendMessage returned not ok")


def telegram_get_me(token: str) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/getMe"
    resp = requests.get(url, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"Telegram getMe HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram getMe returned not ok")
    result = data.get("result")
    if isinstance(result, dict):
        return result
    return {}


def handle_message(
    message: Dict[str, Any],
    state: Dict[str, Any],
    options: Dict[str, Any],
    style_cache_reply: StyleCache,
    client: OpenAI,
) -> bool:
    if not is_group_message(message):
        return False

    chat = message.get("chat") or {}
    chat_id = int(chat.get("id"))

    if not is_allowed_chat(chat_id, options["allowed_chat_ids"]):
        return False

    now_ts = int(time.time())
    record_activity(state, chat_id, now_ts, options["activity_window_seconds"])
    chat_state = get_chat_state(state, chat_id)
    count, per_min = activity_metrics(chat_state, options["activity_window_seconds"])
    logging.info("Activity chat=%s count=%d per_min=%.2f", mask_chat_id(chat_id), count, per_min)

    text = message_text(message)
    mentioned = is_mention(text, options["bot_username"])
    replied = is_reply_to_bot(message, options["bot_username"])
    msg_id = int(message.get("message_id", 0) or 0)
    logging.info(
        "Message flags chat=%s msg=%d mention=%s reply_to_bot=%s reply_enabled=%s",
        mask_chat_id(chat_id),
        msg_id,
        mentioned,
        replied,
        options["reply_on_mention"],
    )

    if not options["reply_on_mention"]:
        logging.info("Skip reply chat=%s msg=%d reason=reply_disabled", mask_chat_id(chat_id), msg_id)
        return True

    if not (mentioned or replied):
        logging.info("Skip reply chat=%s msg=%d reason=no_mention_or_reply", mask_chat_id(chat_id), msg_id)
        return True

    block_reason = reply_block_reason(chat_state, options["max_posts_per_day"])
    if not can_post_reply(chat_state, options["max_posts_per_day"]):
        logging.info("Skip reply chat=%s msg=%d reason=%s", mask_chat_id(chat_id), msg_id, block_reason or "blocked")
        return True

    reply_msg = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else None
    reply_text = message_text(reply_msg) if reply_msg else ""

    try:
        style_text = style_cache_reply.get()
        response_text, response_meta = create_openai_reply(client, options["openai_model"], style_text, text, reply_text)
        if response_text:
            telegram_send_message(
                options["telegram_bot_token"],
                chat_id,
                response_text,
                reply_to_message_id=message.get("message_id"),
            )
            register_post(chat_state, now_ts)
            logging.info("Mention/Reply response posted chat=%s", mask_chat_id(chat_id))
        else:
            logging.warning(
                "OpenAI returned empty reply chat=%s msg=%d %s",
                mask_chat_id(chat_id),
                msg_id,
                response_meta,
            )
    except Exception as exc:
        logging.error("Mention/Reply processing failed: %s", summarize_exception(exc))

    return True


def maybe_post_ambient(
    chat_id: int,
    state: Dict[str, Any],
    options: Dict[str, Any],
    style_cache_post: StyleCache,
    client: OpenAI,
) -> bool:
    if not options["ambient_enabled"]:
        return False

    now_ts = int(time.time())
    chat_state = get_chat_state(state, chat_id)
    prune_activity(chat_state, now_ts, options["activity_window_seconds"])
    count, per_min = activity_metrics(chat_state, options["activity_window_seconds"])

    if count < options["activity_min_msgs_per_window"]:
        return False

    if not can_post_now(
        chat_state,
        now_ts,
        options["min_seconds_between_posts"],
        options["max_posts_per_day"],
    ):
        return False

    prob = compute_ambient_probability(per_min)
    if random.random() >= prob:
        return False

    try:
        style_text = style_cache_post.get()
        ambient_text, response_meta = create_openai_ambient(client, options["openai_model"], style_text, count, per_min)
        if ambient_text:
            telegram_send_message(options["telegram_bot_token"], chat_id, ambient_text)
            register_post(chat_state, now_ts)
            logging.info(
                "Ambient post chat=%s count=%d per_min=%.2f prob=%.3f",
                mask_chat_id(chat_id),
                count,
                per_min,
                prob,
            )
            return True
        logging.warning("OpenAI returned empty ambient chat=%s %s", mask_chat_id(chat_id), response_meta)
    except Exception as exc:
        logging.error("Ambient post failed: %s", summarize_exception(exc))

    return False


def ambient_candidate_chat_ids(state: Dict[str, Any], options: Dict[str, Any]) -> List[int]:
    if options["allowed_chat_ids"]:
        return sorted(options["allowed_chat_ids"])

    ids: List[int] = []
    for key in state.get("chats", {}).keys():
        try:
            ids.append(int(key))
        except ValueError:
            continue
    return sorted(ids)


def run() -> None:
    setup_logging()
    logging.info("Starting telegram_activity_bot")

    options = load_options()
    if not options["bot_username"]:
        try:
            me = telegram_get_me(options["telegram_bot_token"])
            username = me.get("username")
            if isinstance(username, str) and username.strip():
                options["bot_username"] = normalize_bot_username(username)
                logging.info("Loaded bot username from Telegram getMe")
            else:
                logging.warning("Bot username is empty; mention detection may not work")
        except Exception as exc:
            logging.warning("Could not auto-load bot username: %s", summarize_exception(exc))

    if options["allowed_chat_ids"]:
        logging.info("Allowed chats configured: %d", len(options["allowed_chat_ids"]))
    else:
        logging.info("Allowed chats empty: tracking all chats")
    logging.info(
        "Runtime options model=%s reply_on_mention=%s ambient_enabled=%s window=%ds min_msgs=%d cooldown=%ds max_posts_per_day=%d style_post=%s style_reply=%s",
        options["openai_model"],
        options["reply_on_mention"],
        options["ambient_enabled"],
        options["activity_window_seconds"],
        options["activity_min_msgs_per_window"],
        options["min_seconds_between_posts"],
        options["max_posts_per_day"],
        options["style_post_filename"],
        options["style_reply_filename"],
    )

    state = load_state()
    style_cache_post = StyleCache(options["style_post_filename"], options["style_reload_seconds"])
    style_cache_reply = StyleCache(options["style_reply_filename"], options["style_reload_seconds"])
    client = OpenAI(api_key=options["openai_api_key"])

    backoff = 2
    next_ambient_ts = time.time() + AMBIENT_TICK_SECONDS

    while True:
        state_changed = False

        try:
            now = time.time()
            poll_timeout = int(max(1, min(POLL_TIMEOUT_SECONDS, next_ambient_ts - now)))

            updates = telegram_get_updates(
                options["telegram_bot_token"],
                int(state.get("telegram_offset", 0)),
                poll_timeout,
            )

            if updates:
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    current_offset = int(state.get("telegram_offset", 0))
                    if update_id >= current_offset:
                        state["telegram_offset"] = update_id + 1
                        state_changed = True

                    message = update.get("message")
                    if isinstance(message, dict):
                        if handle_message(message, state, options, style_cache_reply, client):
                            state_changed = True

            if time.time() >= next_ambient_ts:
                for chat_id in ambient_candidate_chat_ids(state, options):
                    if maybe_post_ambient(chat_id, state, options, style_cache_post, client):
                        state_changed = True
                next_ambient_ts = time.time() + AMBIENT_TICK_SECONDS

            if state_changed:
                save_state(state)

            backoff = 2

        except KeyboardInterrupt:
            logging.info("Stopping telegram_activity_bot")
            break
        except Exception as exc:
            logging.error("Main loop error: %s", summarize_exception(exc))
            if state_changed:
                try:
                    save_state(state)
                except Exception:
                    pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    run()
