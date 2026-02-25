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
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CHUNK_SIZE = 3900
MAX_ACTIVITY_ENTRIES_PER_CHAT = 10000
MAX_STYLE_CHARS = 20000
MAX_MEMORY_CHARS = 5000
MAX_MENTION_CONTEXT_CHARS = 1000
MAX_REPLY_CONTEXT_CHARS = 500
MAX_AMBIENT_MEMORY_CHARS = 600
MEMORY_FILENAME = "memory.md"
LAST_AMBIENT_HEADER = "## Last Ambient Post"
NICKS_FILENAME = "nicks.md"
AMBIENT_NICK_LINES = 100
AMBIENT_POST_SEND_PROBABILITY = 0.30


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)


def summarize_exception(exc: Exception) -> str:
    name = exc.__class__.__name__
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        resp = getattr(exc, "response", None)
        if resp is not None:
            status_code = getattr(resp, "status_code", None)

    details: List[str] = []
    if isinstance(status_code, int):
        details.append(f"status={status_code}")

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            err_type = err.get("type")
            err_code = err.get("code")
            err_param = err.get("param")
            if isinstance(err_type, str) and err_type:
                details.append(f"type={err_type}")
            if isinstance(err_code, str) and err_code:
                details.append(f"code={err_code}")
            if isinstance(err_param, str) and err_param:
                details.append(f"param={err_param}")

    if details:
        return f"{name}(" + ",".join(details) + ")"
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
        "openai_model": "gpt-5.2-chat-latest",
        "allowed_chat_ids": "",
        "admin_user_ids": "",
        "bot_username": "",
        "style_post_filename": "style_post.md",
        "style_reply_filename": "style_reply.md",
        "style_reload_seconds": 60,
        "activity_window_seconds": 300,
        "activity_min_msgs_per_window": 3,
        "ambient_enabled": True,
        "min_seconds_between_posts": 120,
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
    merged["openai_model"] = (
        str(merged.get("openai_model", "gpt-5.2-chat-latest")).strip() or "gpt-5.2-chat-latest"
    )
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
    merged["min_seconds_between_posts"] = max(0, int(merged.get("min_seconds_between_posts", 120)))
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
        "last_ambient_post_ts": 0,
        "ambient_daily_count": 0,
        "ambient_daily_date": "",
        "last_reply_post_ts": 0,
        "reply_daily_count": 0,
        "reply_daily_date": "",
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
    if "last_ambient_post_ts" not in chat:
        chat["last_ambient_post_ts"] = int(chat.get("last_post_ts", 0) or 0)
    if "ambient_daily_count" not in chat:
        chat["ambient_daily_count"] = int(chat.get("daily_count", 0) or 0)
    if "ambient_daily_date" not in chat:
        chat["ambient_daily_date"] = str(chat.get("daily_date", "") or "")
    if "last_reply_post_ts" not in chat:
        chat["last_reply_post_ts"] = 0
    if "reply_daily_count" not in chat:
        chat["reply_daily_count"] = 0
    if "reply_daily_date" not in chat:
        chat["reply_daily_date"] = ""
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


def can_post_ambient(chat_state: Dict[str, Any], now_ts: int, min_seconds_between_posts: int, max_posts_per_day: int) -> bool:
    last_ambient_post_ts = int(chat_state.get("last_ambient_post_ts", 0) or 0)
    if now_ts - last_ambient_post_ts < min_seconds_between_posts:
        return False

    if max_posts_per_day > 0:
        d = today_utc()
        if chat_state.get("ambient_daily_date") != d:
            chat_state["ambient_daily_date"] = d
            chat_state["ambient_daily_count"] = 0
        if int(chat_state.get("ambient_daily_count", 0) or 0) >= max_posts_per_day:
            return False

    return True


def ambient_block_reason(
    chat_state: Dict[str, Any],
    now_ts: int,
    min_seconds_between_posts: int,
    max_posts_per_day: int,
) -> str:
    last_ambient_post_ts = int(chat_state.get("last_ambient_post_ts", 0) or 0)
    since_last = now_ts - last_ambient_post_ts
    if since_last < min_seconds_between_posts:
        remaining = min_seconds_between_posts - since_last
        return f"cooldown({remaining}s)"

    if max_posts_per_day > 0:
        d = today_utc()
        if chat_state.get("ambient_daily_date") == d:
            daily_count = int(chat_state.get("ambient_daily_count", 0) or 0)
            if daily_count >= max_posts_per_day:
                return "daily_cap"

    return ""


def register_ambient_post(chat_state: Dict[str, Any], now_ts: int) -> None:
    chat_state["last_ambient_post_ts"] = now_ts
    d = today_utc()
    if chat_state.get("ambient_daily_date") != d:
        chat_state["ambient_daily_date"] = d
        chat_state["ambient_daily_count"] = 0
    chat_state["ambient_daily_count"] = int(chat_state.get("ambient_daily_count", 0) or 0) + 1


def register_reply_post(chat_state: Dict[str, Any], now_ts: int) -> None:
    chat_state["last_reply_post_ts"] = now_ts
    d = today_utc()
    if chat_state.get("reply_daily_date") != d:
        chat_state["reply_daily_date"] = d
        chat_state["reply_daily_count"] = 0
    chat_state["reply_daily_count"] = int(chat_state.get("reply_daily_count", 0) or 0) + 1


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


def sender_label(message: Optional[Dict[str, Any]]) -> str:
    if not isinstance(message, dict):
        return "unknown"
    frm = message.get("from")
    if not isinstance(frm, dict):
        return "unknown"

    username = frm.get("username")
    if isinstance(username, str) and username.strip():
        uname = username.strip()
        if not uname.startswith("@"):
            uname = f"@{uname}"
        return uname

    user_id = frm.get("id")
    if isinstance(user_id, int):
        return f"id:{user_id}"
    return "unknown"


def memory_file_path() -> str:
    return os.path.join(CONFIG_DIR, MEMORY_FILENAME)


def load_memory_text() -> str:
    path = memory_file_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read(MAX_MEMORY_CHARS + 1)
        return clamp_text(content, MAX_MEMORY_CHARS).strip()
    except FileNotFoundError:
        return ""
    except Exception as exc:
        logging.warning("Failed reading memory file: %s", summarize_exception(exc))
        return ""


def save_memory_text(content: str) -> bool:
    path = memory_file_path()
    normalized = clamp_text(content or "", MAX_MEMORY_CHARS).strip()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(normalized)
        return True
    except Exception as exc:
        logging.warning("Failed writing memory file: %s", summarize_exception(exc))
        return False


def ensure_memory_file_exists() -> None:
    path = memory_file_path()
    if os.path.exists(path):
        return
    if save_memory_text(""):
        logging.info("Created memory file at /config/%s", MEMORY_FILENAME)


def extract_last_ambient_from_memory(memory_text: str) -> str:
    text = (memory_text or "").strip()
    if not text:
        return ""
    marker = f"\n{LAST_AMBIENT_HEADER}\n"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    if text.startswith(f"{LAST_AMBIENT_HEADER}\n"):
        return text[len(f"{LAST_AMBIENT_HEADER}\n") :].strip()
    return ""


def upsert_last_ambient_in_memory(memory_text: str, ambient_text: str) -> str:
    ambient = clamp_text((ambient_text or "").strip(), MAX_AMBIENT_MEMORY_CHARS).strip()
    if not ambient:
        return clamp_text((memory_text or "").strip(), MAX_MEMORY_CHARS).strip()

    marker = f"\n{LAST_AMBIENT_HEADER}\n"
    base = (memory_text or "").strip()
    if marker in base:
        base = base.split(marker, 1)[0].strip()
    elif base.startswith(f"{LAST_AMBIENT_HEADER}\n"):
        base = ""

    ambient_section = f"{LAST_AMBIENT_HEADER}\n{ambient}"
    if len(ambient_section) >= MAX_MEMORY_CHARS:
        return clamp_text(ambient_section, MAX_MEMORY_CHARS).strip()

    available_for_base = MAX_MEMORY_CHARS - len(ambient_section)
    if base:
        base = clamp_text(base, max(0, available_for_base - 2)).strip()

    if base:
        combined = f"{base}\n\n{ambient_section}"
    else:
        combined = ambient_section
    return clamp_text(combined, MAX_MEMORY_CHARS).strip()


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

    def invalidate(self) -> None:
        self._last_load = 0.0


def refresh_style_post_with_random_nicks(style_post_filename: str) -> bool:
    style_path = os.path.join(CONFIG_DIR, style_post_filename)
    nicks_path = os.path.join(os.path.dirname(style_path), NICKS_FILENAME)

    try:
        with open(style_path, "r", encoding="utf-8") as f:
            style_lines = f.read().splitlines()
    except FileNotFoundError:
        logging.warning("Style post file missing at /config/%s", style_post_filename)
        return False
    except Exception as exc:
        logging.warning("Failed reading style post file: %s", summarize_exception(exc))
        return False

    try:
        with open(nicks_path, "r", encoding="utf-8") as f:
            nick_candidates = [line.strip() for line in f.read().splitlines() if line.strip()]
    except FileNotFoundError:
        logging.warning("Nicks file missing at /config/%s", NICKS_FILENAME)
        return False
    except Exception as exc:
        logging.warning("Failed reading nicks file: %s", summarize_exception(exc))
        return False

    if not nick_candidates:
        logging.warning("Nicks file is empty at /config/%s", NICKS_FILENAME)
        return False

    if len(nick_candidates) >= AMBIENT_NICK_LINES:
        sampled = random.sample(nick_candidates, AMBIENT_NICK_LINES)
    else:
        sampled = [random.choice(nick_candidates) for _ in range(AMBIENT_NICK_LINES)]

    preserved = style_lines[:4]
    rebuilt_lines = preserved + [""] + sampled
    rebuilt_text = "\n".join(rebuilt_lines).rstrip() + "\n"

    try:
        with open(style_path, "w", encoding="utf-8") as f:
            f.write(rebuilt_text)
        logging.info(
            "Refreshed style post with sampled nicks base_lines=%d sampled=%d",
            len(preserved),
            len(sampled),
        )
        return True
    except Exception as exc:
        logging.warning("Failed writing style post file: %s", summarize_exception(exc))
        return False


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


def response_output_debug(response: Any, max_items: int = 5) -> str:
    output = get_field(response, "output")
    if not isinstance(output, list):
        return "output_summary=none"

    chunks: List[str] = []
    for idx, item in enumerate(output[:max_items]):
        item_type = get_field(item, "type")
        item_type_str = item_type if isinstance(item_type, str) and item_type else "unknown"

        content = get_field(item, "content")
        content_count = len(content) if isinstance(content, list) else 0
        content_types: List[str] = []
        text_blocks = 0
        text_chars = 0
        if isinstance(content, list):
            for block in content:
                block_type = get_field(block, "type")
                if isinstance(block_type, str) and block_type:
                    content_types.append(block_type)
                text_val = extract_text_value(get_field(block, "text"))
                if text_val:
                    text_blocks += 1
                    text_chars += len(text_val)

        action = get_field(item, "action")
        action_sources = get_field(action, "sources")
        source_count = len(action_sources) if isinstance(action_sources, list) else 0

        uniq_types = sorted(set(content_types))
        type_fragment = ",".join(uniq_types[:3]) if uniq_types else "-"
        chunks.append(
            f"{idx}:{item_type_str}(content={content_count},text_blocks={text_blocks},text_chars={text_chars},types={type_fragment},sources={source_count})"
        )

    if len(output) > max_items:
        chunks.append(f"+{len(output)-max_items}more")

    return "output_summary=" + ";".join(chunks)


def response_incomplete_reason(response: Any) -> str:
    incomplete = get_field(response, "incomplete_details")
    if incomplete is None:
        return ""
    reason_val = get_field(incomplete, "reason")
    if isinstance(reason_val, str):
        return reason_val
    return ""


def extract_web_sources(response: Any, max_sources: int = 12) -> List[str]:
    output = get_field(response, "output")
    if not isinstance(output, list):
        return []

    seen: Set[str] = set()
    urls: List[str] = []
    for item in output:
        if get_field(item, "type") != "web_search_call":
            continue
        action = get_field(item, "action")
        sources = get_field(action, "sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            url = get_field(source, "url")
            if isinstance(url, str):
                u = url.strip()
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
                    if len(urls) >= max_sources:
                        return urls
    return urls


def format_sources_block(urls: List[str]) -> str:
    if not urls:
        return ""
    lines = "\n".join(f"- {u}" for u in urls)
    return "\n\nQuellen:\n" + lines


def create_response(
    client: OpenAI,
    model: str,
    prompt: str,
    max_output_tokens: int,
    use_tools: bool = False,
    include_sources: bool = False,
) -> Any:
    kwargs: Dict[str, Any] = {
        "model": model,
        "store": False,
        "input": prompt,
        "reasoning": {"effort": "medium"},
        "max_output_tokens": max_output_tokens,
    }
    if use_tools:
        kwargs["tools"] = [{"type": "web_search"}]
        kwargs["tool_choice"] = "auto"
        if include_sources:
            kwargs["include"] = ["web_search_call.action.sources"]
    return client.responses.create(**kwargs)


def call_openai_text(
    client: OpenAI,
    model: str,
    prompt: str,
    max_tokens: int,
    retry_max_tokens: int,
    use_tools: bool = False,
    include_sources: bool = False,
) -> Tuple[str, str]:
    response = create_response(client, model, prompt, max_tokens, use_tools=use_tools, include_sources=include_sources)
    text = extract_response_text(response)
    if text:
        return text, response_debug_meta(response)

    if response_incomplete_reason(response) == "max_output_tokens" and retry_max_tokens > max_tokens:
        retry = create_response(client, model, prompt, retry_max_tokens, use_tools=use_tools, include_sources=include_sources)
        retry_text = extract_response_text(retry)
        if retry_text:
            return retry_text, response_debug_meta(retry)
        return "", f"{response_debug_meta(retry)} {response_output_debug(retry)}"

    return "", f"{response_debug_meta(response)} {response_output_debug(response)}"


def create_openai_reply(
    client: OpenAI,
    model: str,
    style_text: str,
    memory_text: str,
    sender_name: str,
    mention_text: str,
    reply_sender_name: str,
    reply_text: str,
) -> Tuple[str, str, str]:
    mention_text = clamp_text(mention_text, MAX_MENTION_CONTEXT_CHARS)
    reply_text = clamp_text(reply_text, MAX_REPLY_CONTEXT_CHARS)

    prompt = (
        "You are a concise Telegram group assistant. "
        "Follow the style notes exactly. "
        "Keep output short and safe."
        "\n\nStyle notes:\n"
        f"{style_text or '(none)'}"
        f"\n\nPersistent memory notes (max {MAX_MEMORY_CHARS} chars):\n"
        f"{memory_text or '(empty)'}"
        "\n\nTask: Reply to the user message."
        "\nSender:\n"
        f"{sender_name or 'unknown'}"
        "\nUser message:\n"
        f"{mention_text or '(empty)'}"
    )

    if reply_text:
        prompt += (
            "\n\nReplied-to sender:\n"
            + (reply_sender_name or "unknown")
            + "\nReplied-to message:\n"
            + reply_text
        )

    response = create_response(client, model, prompt, 220, use_tools=True, include_sources=True)
    sources = extract_web_sources(response)
    extracted_text = extract_response_text(response)
    if extracted_text:
        posted_text = extracted_text + format_sources_block(sources)
        return posted_text, f"{response_debug_meta(response)} sources={len(sources)}", extracted_text

    if response_incomplete_reason(response) == "max_output_tokens":
        follow_prompt = (
            prompt
            + "\n\nNow provide only the final user-facing reply text. "
            + "Keep it short and do not call tools."
        )
        follow_text, follow_meta = call_openai_text(
            client,
            model,
            follow_prompt,
            max_tokens=420,
            retry_max_tokens=520,
            use_tools=False,
            include_sources=False,
        )
        if follow_text:
            posted_text = follow_text + format_sources_block(sources)
            return posted_text, f"{follow_meta} sources={len(sources)}", follow_text
        return "", f"{follow_meta} sources={len(sources)}", ""

    return "", f"{response_debug_meta(response)} sources={len(sources)} {response_output_debug(response)}", ""


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

    return call_openai_text(client, model, prompt, max_tokens=100, retry_max_tokens=180, use_tools=False, include_sources=False)


def create_updated_memory(
    client: OpenAI,
    model: str,
    memory_text: str,
    style_reply_text: str,
    sender_name: str,
    mention_text: str,
    reply_sender_name: str,
    reply_text: str,
    bot_reply_text: str,
) -> Tuple[str, str]:
    mention_text = clamp_text(mention_text, MAX_MENTION_CONTEXT_CHARS)
    reply_text = clamp_text(reply_text, MAX_REPLY_CONTEXT_CHARS)
    bot_reply_text = clamp_text(bot_reply_text, 400)

    prompt = (
        "You maintain a short markdown memory for a Telegram bot. "
        "Update memory with only stable, useful facts or preferences. "
        "Do not include secrets. "
        f"Keep the result at most {MAX_MEMORY_CHARS} characters."
        "\nReturn only the full updated memory markdown, nothing else."
        "\n\nReply style notes:\n"
        f"{style_reply_text or '(none)'}"
        "\n\nCurrent memory:\n"
        f"{memory_text or '(empty)'}"
        "\n\nLatest interaction:"
        "\nSender:\n"
        f"{sender_name or 'unknown'}"
        "\nUser message:\n"
        f"{mention_text or '(empty)'}"
    )

    if reply_text:
        prompt += (
            "\nReplied-to sender:\n"
            + (reply_sender_name or "unknown")
            + "\nReplied-to message:\n"
            + reply_text
        )

    prompt += "\nBot reply:\n" + (bot_reply_text or "(empty)")

    updated_text, meta = call_openai_text(client, model, prompt, max_tokens=240, retry_max_tokens=420)
    return clamp_text(updated_text, MAX_MEMORY_CHARS).strip(), meta


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


def telegram_send_message_chunks(token: str, chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
    if not text:
        return
    if len(text) <= TELEGRAM_TEXT_LIMIT:
        telegram_send_message(token, chat_id, text, reply_to_message_id=reply_to_message_id)
        return

    parts = [text[i : i + TELEGRAM_CHUNK_SIZE] for i in range(0, len(text), TELEGRAM_CHUNK_SIZE)]
    for idx, part in enumerate(parts):
        if idx == 0:
            telegram_send_message(token, chat_id, part, reply_to_message_id=reply_to_message_id)
        else:
            telegram_send_message(token, chat_id, part)


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

    reply_msg = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else None
    reply_text = message_text(reply_msg) if reply_msg else ""
    sender_name = sender_label(message)
    reply_sender_name = sender_label(reply_msg)

    try:
        style_text = style_cache_reply.get()
        memory_text = load_memory_text()
        response_payload, response_meta, response_text = create_openai_reply(
            client,
            options["openai_model"],
            style_text,
            memory_text,
            sender_name,
            text,
            reply_sender_name,
            reply_text,
        )
        if response_payload:
            telegram_send_message_chunks(
                options["telegram_bot_token"],
                chat_id,
                response_payload,
                reply_to_message_id=message.get("message_id"),
            )
            register_reply_post(chat_state, now_ts)
            logging.info("Mention/Reply response posted chat=%s", mask_chat_id(chat_id))
            updated_memory, memory_meta = create_updated_memory(
                client,
                options["openai_model"],
                memory_text,
                style_text,
                sender_name,
                text,
                reply_sender_name,
                reply_text,
                response_text,
            )
            last_ambient_post = extract_last_ambient_from_memory(memory_text)
            if last_ambient_post:
                updated_memory = upsert_last_ambient_in_memory(updated_memory, last_ambient_post)
            if updated_memory and updated_memory != memory_text:
                if save_memory_text(updated_memory):
                    logging.info(
                        "Memory updated chat=%s msg=%d chars=%d",
                        mask_chat_id(chat_id),
                        msg_id,
                        len(updated_memory),
                    )
                else:
                    logging.warning("Memory update skipped chat=%s msg=%d", mask_chat_id(chat_id), msg_id)
            elif not updated_memory:
                logging.warning("OpenAI returned empty memory update chat=%s msg=%d %s", mask_chat_id(chat_id), msg_id, memory_meta)
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

    if not can_post_ambient(
        chat_state,
        now_ts,
        options["min_seconds_between_posts"],
        options["max_posts_per_day"],
    ):
        reason = ambient_block_reason(
            chat_state,
            now_ts,
            options["min_seconds_between_posts"],
            options["max_posts_per_day"],
        )
        logging.info("Skip ambient chat=%s reason=%s", mask_chat_id(chat_id), reason or "blocked")
        return False

    if random.random() >= AMBIENT_POST_SEND_PROBABILITY:
        register_ambient_post(chat_state, now_ts)
        logging.info(
            "Ambient skipped by send gate chat=%s gate_prob=%.2f",
            mask_chat_id(chat_id),
            AMBIENT_POST_SEND_PROBABILITY,
        )
        return True

    try:
        if refresh_style_post_with_random_nicks(options["style_post_filename"]):
            style_cache_post.invalidate()
        style_text = style_cache_post.get()
        ambient_text, response_meta = create_openai_ambient(client, options["openai_model"], style_text, count, per_min)
        if ambient_text:
            telegram_send_message(options["telegram_bot_token"], chat_id, ambient_text)
            register_ambient_post(chat_state, now_ts)
            memory_before = load_memory_text()
            memory_after = upsert_last_ambient_in_memory(memory_before, ambient_text)
            if memory_after != memory_before:
                if save_memory_text(memory_after):
                    logging.info(
                        "Memory last ambient updated chat=%s chars=%d",
                        mask_chat_id(chat_id),
                        len(memory_after),
                    )
                else:
                    logging.warning("Memory last ambient update skipped chat=%s", mask_chat_id(chat_id))
            logging.info(
                "Ambient post chat=%s count=%d per_min=%.2f gate_prob=%.2f",
                mask_chat_id(chat_id),
                count,
                per_min,
                AMBIENT_POST_SEND_PROBABILITY,
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
    ensure_memory_file_exists()
    logging.info(
        "Runtime options model=%s reply_on_mention=%s ambient_enabled=%s window=%ds min_msgs=%d ambient_cooldown=%ds ambient_max_posts_per_day=%d style_post=%s style_reply=%s memory=%s",
        options["openai_model"],
        options["reply_on_mention"],
        options["ambient_enabled"],
        options["activity_window_seconds"],
        options["activity_min_msgs_per_window"],
        options["min_seconds_between_posts"],
        options["max_posts_per_day"],
        options["style_post_filename"],
        options["style_reply_filename"],
        MEMORY_FILENAME,
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
