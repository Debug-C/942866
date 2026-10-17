from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import traceback
from typing import Any, Awaitable, Callable

# --- Сторонние библиотеки ---
import aiosqlite
from aiogram import Bot, Dispatcher, BaseMiddleware, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramUnauthorizedError,
    TelegramForbiddenError,
)
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.token import TokenValidationError

# ===========================================================================
# НАСТРОЙКИ БОТА — отредактируй под себя
# ===========================================================================

# Токен бота от @BotFather.
# Получить: открой @BotFather → /newbot → скопируй токен.
# Токен выглядит так: 123456789:AAEhBPxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# ⚠️  ОБЯЗАТЕЛЬНО ЗАМЕНИ ПЛЕЙСХОЛДЕР НИЖЕ НА СВОЙ РЕАЛЬНЫЙ ТОКЕН!  ⚠️
BOT_TOKEN = "8905511808:AAF25_FrxRB2XbBkKiIblOb4bdVhMXJlAzY"

# ID основного канала, куда публикуются анонимные посты.
#   • Числовой ID: -1001234567890 (для приватных каналов)
#   • Или @username публичного канала
# Узнать ID: переслать любое сообщение из канала в @userinfobot
CHANNEL_ID: int | str = -1003894779654

# ID супер-админов (через запятую в квадратных скобках).
# Супер-админы могут добавлять/удалять модераторов через /addmod, /delmod.
# Также они считаются модераторами.
# Узнать свой ID: открой @userinfobot и пришли любое сообщение.
# ⚠️  ЗАМЕНИ НА СВОЙ РЕАЛЬНЫЙ USER-ID!  ⚠️
SUPER_ADMINS: list[int] = [5941778563]

# Модераторы добавляются только через команду /addmod <id> (только супер-админ).
# Они хранятся в БД (таблица moderators). В коде список не задаётся.

# Опционально: URL webhook. Если пусто — бот работает в polling-режиме.
WEBHOOK_URL = ""
WEBHOOK_PORT = 8080

# Путь к файлу SQLite. Директория создастся автоматически.
DB_PATH = "data/bot.db"


# ===========================================================================
# ЛОГИРОВАНИЕ
# ===========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("school-bot")


# ===========================================================================
# ВАЛИДАЦИЯ КОНФИГА — выполняется в main() при запуске
# ===========================================================================

def _validate_config() -> list[str]:
    """Возвращает список ошибок конфигурации. Пустой список = всё ок."""
    errors: list[str] = []
    if not BOT_TOKEN or ":" not in BOT_TOKEN or "ВСТАВЬ" in BOT_TOKEN:
        errors.append(
            "• BOT_TOKEN не задан или некорректен.\n"
            "  Открой @BotFather → /newbot → скопируй токен вида\n"
            "  123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxx и впиши в BOT_TOKEN\n"
            "  в начале bot.py."
        )
    if not CHANNEL_ID:
        errors.append(
            "• CHANNEL_ID не задан. Укажи ID канала (-1001234567890)\n"
            "  или @username публичного канала в CHANNEL_ID в начале bot.py."
        )
    if not SUPER_ADMINS:
        errors.append(
            "• SUPER_ADMINS пуст. Впиши свой user-ID (узнать через @userinfobot)\n"
            "  в SUPER_ADMINS в начале bot.py."
        )
    return errors


# ===========================================================================
# БАЗА ДАННЫХ (SQLite через aiosqlite)
# ===========================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT NOT NULL DEFAULT '',
    first_name     TEXT NOT NULL DEFAULT '',
    last_name      TEXT NOT NULL DEFAULT '',
    submissions_n  INTEGER NOT NULL DEFAULT 0,
    rejected_n     INTEGER NOT NULL DEFAULT 0,
    banned         INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS submissions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id        INTEGER NOT NULL,
    author_username  TEXT NOT NULL DEFAULT '',
    author_name      TEXT NOT NULL DEFAULT '',
    content_type     TEXT NOT NULL,
    caption          TEXT NOT NULL DEFAULT '',
    extra_data       TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    moderated_by     INTEGER,
    moderated_at     TEXT,
    published_msg_id INTEGER,
    published_at     TEXT,
    rejection_reason TEXT NOT NULL DEFAULT '',
    mod_message_id   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sub_status   ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_sub_author   ON submissions(author_id);

-- Каждая отправка поста в ЛС конкретному модератору.
-- При действии модератора — снимаем кнопки у всех остальных записей.
CREATE TABLE IF NOT EXISTS submission_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id  INTEGER NOT NULL,
    moderator_id   INTEGER NOT NULL,
    chat_id        INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (submission_id) REFERENCES submissions(id)
);

CREATE INDEX IF NOT EXISTS idx_sm_sub  ON submission_messages(submission_id);

CREATE TABLE IF NOT EXISTS moderators (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    added_by   INTEGER,
    added_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    moderator_id  INTEGER,
    action         TEXT NOT NULL,
    submission_id  INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def init_db() -> None:
    """Создаёт файл БД и схему."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def get_db() -> aiosqlite.Connection:
    return aiosqlite.connect(DB_PATH)


async def fetch_one(query: str, params: tuple = ()) -> dict[str, Any] | None:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def fetch_all(query: str, params: tuple = ()) -> list[dict[str, Any]]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def execute(query: str, params: tuple = ()) -> int | None:
    async with get_db() as db:
        cur = await db.execute(query, params)
        await db.commit()
        return cur.lastrowid


# ===========================================================================
# FSM-СОСТОЯНИЯ (только для добавления модератора через /addmod)
# ===========================================================================


class AddModeratorForm(StatesGroup):
    waiting_user_id = State()


# ===========================================================================
# INLINE КЛАВИАТУРЫ
# ===========================================================================


def moderation_kb(submission_id: int) -> InlineKeyboardMarkup:
    """Клавиатура модерации под сообщением в ЛС модератора.

    Без категорий, без причин отказа — максимально просто.
    ❌ Отклонить сразу отклоняет без выбора причины.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Опубликовать", callback_data=f"approve:{submission_id}")
    kb.button(text="❌ Отклонить", callback_data=f"reject:{submission_id}")
    kb.button(text="👤 Профиль", callback_data=f"profile:{submission_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить модератора", callback_data="admin:addmod")
    kb.button(text="📋 Список модераторов", callback_data="admin:listmod")
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.adjust(1)
    return kb.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    """Используется только в FSM-режиме (добавление модератора)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Отмена", callback_data="admin:cancel")
    return kb.as_markup()


# ===========================================================================
# СКЛЕЙКА АЛЬБОМОВ (middleware)
# ===========================================================================

ALBUM_WAIT_SECONDS = 0.7


class Album:
    """Контейнер склеенного альбома. Кладётся в data['album']."""

    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages
        first = messages[0]
        self.from_user = first.from_user
        self.chat = first.chat
        self.media_group_id = first.media_group_id
        self.caption = first.caption or ""

    @property
    def items(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for m in self.messages:
            if m.photo:
                out.append({"type": "photo", "file_id": m.photo[-1].file_id})
            elif m.video:
                out.append({"type": "video", "file_id": m.video.file_id})
            elif m.animation:
                out.append({"type": "animation", "file_id": m.animation.file_id})
        return out


class AlbumMiddleware(BaseMiddleware):
    """Склеивает сообщения с одинаковым media_group_id.

    Баг-фикс: проверяем data['album'] в начале — иначе повторный заход
    middleware на пропихнутое сообщение создаёт бесконечный цикл.
    """

    def __init__(self) -> None:
        self._buffers: dict[tuple[int, str], dict[str, Any]] = {}

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if data.get("album"):
            return await handler(event, data)

        if not event.media_group_id:
            return await handler(event, data)

        key = (event.chat.id, event.media_group_id)
        if key not in self._buffers:
            self._buffers[key] = {
                "messages": [event],
                "handler": handler,
                "data": data,
                "task": asyncio.create_task(self._flush(key)),
            }
            return None

        self._buffers[key]["messages"].append(event)
        return None

    async def _flush(self, key: tuple[int, str]) -> None:
        try:
            await asyncio.sleep(ALBUM_WAIT_SECONDS)
            buffer = self._buffers.pop(key, None)
            if not buffer:
                return
            messages: list[Message] = buffer["messages"]
            messages.sort(key=lambda m: m.date)
            album = Album(messages)
            new_data = dict(buffer["data"])
            new_data["album"] = album
            await buffer["handler"](messages[0], new_data)
        except Exception:
            traceback.print_exc()
            self._buffers.pop(key, None)


# ===========================================================================
# СЕРВИСЫ
# ===========================================================================

CONTENT_TYPES = {
    "text", "photo", "video", "video_note",
    "voice", "audio", "animation", "album",
}


# --- Пользователи ---

async def save_user(user_id: int, username: str, first_name: str, last_name: str) -> None:
    await execute(
        """
        INSERT INTO users (user_id, username, first_name, last_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username   = excluded.username,
            first_name = excluded.first_name,
            last_name  = excluded.last_name
        """,
        (user_id, username or "", first_name or "", last_name or ""),
    )


async def is_user_banned(user_id: int) -> bool:
    row = await fetch_one("SELECT banned FROM users WHERE user_id = ?", (user_id,))
    return bool(row and row["banned"])


async def ban_user(user_id: int, banned: bool = True) -> None:
    await execute(
        "UPDATE users SET banned = ? WHERE user_id = ?",
        (1 if banned else 0, user_id),
    )


async def get_user(user_id: int) -> dict[str, Any] | None:
    return await fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))


async def increment_user_counters(
    user_id: int, *, published: bool = False, rejected: bool = False
) -> None:
    if published:
        await execute(
            "UPDATE users SET submissions_n = submissions_n + 1 WHERE user_id = ?",
            (user_id,),
        )
    if rejected:
        await execute(
            "UPDATE users SET rejected_n = rejected_n + 1 WHERE user_id = ?",
            (user_id,),
        )


# --- Submissions ---

async def create_submission(
    *,
    author_id: int,
    author_username: str,
    author_name: str,
    content_type: str,
    caption: str,
    extra_data: dict,
) -> int:
    if content_type not in CONTENT_TYPES:
        raise ValueError(f"Unknown content_type: {content_type}")
    last_id = await execute(
        """
        INSERT INTO submissions
            (author_id, author_username, author_name,
             content_type, caption, extra_data, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            author_id, author_username, author_name,
            content_type, caption,
            json.dumps(extra_data, ensure_ascii=False),
        ),
    )
    return int(last_id or 0)


async def get_submission(submission_id: int) -> dict[str, Any] | None:
    return await fetch_one("SELECT * FROM submissions WHERE id = ?", (submission_id,))


async def list_pending(limit: int = 20) -> list[dict[str, Any]]:
    return await fetch_all(
        """
        SELECT * FROM submissions
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (limit,),
    )


async def update_status(
    submission_id: int,
    status: str,
    *,
    moderator_id: int | None = None,
    published_msg_id: int | None = None,
) -> None:
    fields = ["status = ?"]
    params: list[Any] = [status]
    if moderator_id is not None:
        fields.append("moderated_by = ?")
        params.append(moderator_id)
        fields.append("moderated_at = datetime('now')")
    if published_msg_id is not None:
        fields.append("published_msg_id = ?")
        params.append(published_msg_id)
        fields.append("published_at = datetime('now')")
    params.append(submission_id)
    await execute(
        f"UPDATE submissions SET {', '.join(fields)} WHERE id = ?",
        tuple(params),
    )


async def update_mod_message(submission_id: int, message_id: int) -> None:
    await execute(
        "UPDATE submissions SET mod_message_id = ? WHERE id = ?",
        (message_id, submission_id),
    )


async def get_author_stats(user_id: int) -> dict[str, int]:
    rows = await fetch_all(
        "SELECT status, COUNT(*) AS n FROM submissions WHERE author_id = ? GROUP BY status",
        (user_id,),
    )
    return {r["status"]: r["n"] for r in rows}


async def get_global_stats() -> dict[str, int]:
    rows = await fetch_all(
        "SELECT status, COUNT(*) AS n FROM submissions GROUP BY status"
    )
    out = {r["status"]: r["n"] for r in rows}
    out["total"] = sum(out.values())
    return out


# --- Модераторы ---

def is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMINS


async def is_moderator(user_id: int) -> bool:
    """Модератор = супер-админ ИЛИ в таблице moderators (через /addmod)."""
    if is_super_admin(user_id):
        return True
    row = await fetch_one("SELECT 1 FROM moderators WHERE user_id = ?", (user_id,))
    return bool(row)


async def get_all_moderator_ids() -> list[int]:
    """Все модераторы: SUPER_ADMINS (из кода) + из таблицы moderators (через /addmod)."""
    ids: set[int] = set(SUPER_ADMINS)
    rows = await fetch_all("SELECT user_id FROM moderators")
    for r in rows:
        ids.add(r["user_id"])
    return sorted(ids)


async def add_moderator(user_id: int, username: str, first_name: str, added_by: int) -> None:
    await execute(
        """
        INSERT INTO moderators (user_id, username, first_name, added_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
        """,
        (user_id, username or "", first_name or "", added_by),
    )


async def remove_moderator(user_id: int) -> int:
    row = await fetch_one("SELECT 1 FROM moderators WHERE user_id = ?", (user_id,))
    if not row:
        return 0
    await execute("DELETE FROM moderators WHERE user_id = ?", (user_id,))
    return 1


async def list_moderators_db() -> list[dict[str, Any]]:
    return await fetch_all("SELECT * FROM moderators ORDER BY added_at")


async def log_action(moderator_id: int, action: str, submission_id: int) -> None:
    await execute(
        "INSERT INTO audit_log (moderator_id, action, submission_id) VALUES (?, ?, ?)",
        (moderator_id, action, submission_id),
    )


# --- submission_messages: отправленные в ЛС модераторам ---

async def add_submission_message(
    submission_id: int, moderator_id: int, chat_id: int, message_id: int
) -> None:
    await execute(
        """
        INSERT INTO submission_messages
            (submission_id, moderator_id, chat_id, message_id)
        VALUES (?, ?, ?, ?)
        """,
        (submission_id, moderator_id, chat_id, message_id),
    )


async def get_submission_messages(submission_id: int) -> list[dict[str, Any]]:
    return await fetch_all(
        "SELECT * FROM submission_messages WHERE submission_id = ?",
        (submission_id,),
    )


async def mark_other_messages_done(
    bot: Bot,
    submission_id: int,
    *,
    skip_chat_id: int | None = None,
    skip_message_id: int | None = None,
) -> None:
    """Снимает inline-кнопки у всех сообщений модерации, кроме текущего."""
    msgs = await get_submission_messages(submission_id)
    for m in msgs:
        if skip_chat_id is not None and m["chat_id"] == skip_chat_id \
                and skip_message_id is not None and m["message_id"] == skip_message_id:
            continue
        try:
            await bot.edit_message_reply_markup(
                chat_id=m["chat_id"],
                message_id=m["message_id"],
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
        except TelegramAPIError as exc:
            log.warning("mark_other_messages_done: chat=%s msg=%s err=%s",
                        m["chat_id"], m["message_id"], exc)


# --- Отправка поста в ЛС конкретному модератору ---

async def _send_submission_to_chat(
    bot: Bot,
    chat_id: int | str,
    submission: dict[str, Any],
    caption: str,
    kb: InlineKeyboardMarkup,
) -> Message:
    """Низкоуровневая отправка контента в указанный чат."""
    extra = json.loads(submission["extra_data"] or "{}")
    content_type = submission["content_type"]

    if content_type == "text":
        return await bot.send_message(chat_id, caption, reply_markup=kb)
    if content_type == "photo":
        return await bot.send_photo(chat_id, photo=extra["file_id"], caption=caption, reply_markup=kb)
    if content_type == "video":
        return await bot.send_video(chat_id, video=extra["file_id"], caption=caption, reply_markup=kb)
    if content_type == "video_note":
        # Кружок нельзя с подписью → шлём кружок, затем сообщение с подписью+кнопками
        await bot.send_video_note(chat_id, video_note=extra["file_id"])
        return await bot.send_message(chat_id, caption, reply_markup=kb)
    if content_type == "voice":
        return await bot.send_voice(chat_id, voice=extra["file_id"], caption=caption, reply_markup=kb)
    if content_type == "audio":
        return await bot.send_audio(chat_id, audio=extra["file_id"], caption=caption, reply_markup=kb)
    if content_type == "animation":
        return await bot.send_animation(chat_id, animation=extra["file_id"], caption=caption, reply_markup=kb)
    if content_type == "album":
        from aiogram.types import InputMediaPhoto, InputMediaVideo

        items: list[dict] = extra.get("items", [])
        media = []
        for i, it in enumerate(items):
            cap = caption if i == 0 else None
            if it["type"] == "photo":
                media.append(InputMediaPhoto(media=it["file_id"], caption=cap))
            else:
                media.append(InputMediaVideo(media=it["file_id"], caption=cap))
        if len(media) == 1:
            first = items[0]
            if first["type"] == "photo":
                return await bot.send_photo(chat_id, photo=first["file_id"], caption=caption, reply_markup=kb)
            return await bot.send_video(chat_id, video=first["file_id"], caption=caption, reply_markup=kb)
        await bot.send_media_group(chat_id, media)
        return await bot.send_message(
            chat_id,
            f"📋 Пост #{submission['id']} · нажми кнопку ниже для модерации",
            reply_markup=kb,
        )
    return await bot.send_message(chat_id, caption, reply_markup=kb)


def _build_mod_caption(submission: dict[str, Any]) -> str:
    """Подпись для сообщения модератору — БЕЗ метаданных автора.

    Просто возвращаем текст подписи автора (или заглушку, если подписи нет).
    Никакого ID, username, времени, категорий.
    """
    return submission["caption"] or "(без подписи)"


async def send_to_moderation(
    bot: Bot, submission: dict[str, Any], *, skip_user_id: int | None = None
) -> int:
    """Рассылает пост в ЛС каждому модератору. Возвращает количество успешно отправленных.

    skip_user_id — если задан, этого модератора пропускаем (например, автор сам
    модератор — ему не дублируем пост в ЛС, он уже видит свой пост).
    """
    caption = _build_mod_caption(submission)
    kb = moderation_kb(submission["id"])

    moderator_ids = await get_all_moderator_ids()
    sent_count = 0
    for mod_id in moderator_ids:
        if skip_user_id is not None and mod_id == skip_user_id:
            continue
        try:
            msg = await _send_submission_to_chat(bot, mod_id, submission, caption, kb)
            await add_submission_message(submission["id"], mod_id, mod_id, msg.message_id)
            sent_count += 1
            if len(moderator_ids) > 1:
                await asyncio.sleep(0.05)
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить пост модератору %s: %s", mod_id, exc)
    return sent_count


# --- Анонимная публикация в канале ---

# Заголовок, который бот добавляет в начало caption каждого поста в канале.
# Меняется в одном месте — меняется везде.
CHANNEL_HEADER = "Пост от подписчика:"


def _build_channel_caption(submission: dict[str, Any]) -> str:
    """Текст поста для канала: заголовок + подпись автора.

    Заголовок и контент идут ОДНИМ сообщением — так у поста будет только
    одна кнопка «Прокомментировать» (если у канала включено обсуждение),
    а не две (отдельно на заголовок и отдельно на контент).
    """
    body = submission["caption"] or ""
    if body:
        return f"{CHANNEL_HEADER}\n\n{body}"
    return CHANNEL_HEADER


async def publish_submission(bot: Bot, submission: dict[str, Any]) -> Message | None:
    """Публикует содержимое submission в канал CHANNEL_ID.

    Заголовок «Пост от подписчика:» идёт в начале caption медиа — это
    одно сообщение (а не два), и кнопка «Прокомментировать» будет только
    одна.

    Исключение — кружок (video_note): у него в Telegram нет caption,
    поэтому бот отправляет сам кружок, а подпись с заголовком —
    следующим сообщением.
    """
    extra = json.loads(submission["extra_data"] or "{}")
    caption = _build_channel_caption(submission)
    chat_id = CHANNEL_ID
    content_type = submission["content_type"]

    if content_type == "text":
        return await bot.send_message(chat_id, caption)
    if content_type == "photo":
        return await bot.send_photo(chat_id, photo=extra["file_id"], caption=caption)
    if content_type == "video":
        return await bot.send_video(chat_id, video=extra["file_id"], caption=caption)
    if content_type == "video_note":
        # У кружка нет caption в Telegram → шлём кружок, затем текст с заголовком
        msg = await bot.send_video_note(chat_id, video_note=extra["file_id"])
        # Подпись автора (если есть) — отдельным сообщением с заголовком
        if submission["caption"]:
            await bot.send_message(
                chat_id, f"{CHANNEL_HEADER}\n\n{submission['caption']}"
            )
        else:
            await bot.send_message(chat_id, CHANNEL_HEADER)
        return msg
    if content_type == "voice":
        return await bot.send_voice(chat_id, voice=extra["file_id"], caption=caption)
    if content_type == "audio":
        return await bot.send_audio(chat_id, audio=extra["file_id"], caption=caption)
    if content_type == "animation":
        return await bot.send_animation(chat_id, animation=extra["file_id"], caption=caption)
    if content_type == "album":
        from aiogram.types import InputMediaPhoto, InputMediaVideo

        items: list[dict] = extra.get("items", [])
        media = []
        for i, it in enumerate(items):
            cap = caption if i == 0 else None
            if it["type"] == "photo":
                media.append(InputMediaPhoto(media=it["file_id"], caption=cap))
            else:
                media.append(InputMediaVideo(media=it["file_id"], caption=cap))
        if len(media) == 1:
            first = items[0]
            if first["type"] == "photo":
                return await bot.send_photo(chat_id, photo=first["file_id"], caption=caption)
            return await bot.send_video(chat_id, video=first["file_id"], caption=caption)
        messages = await bot.send_media_group(chat_id, media)
        return messages[0] if messages else None
    return None


# --- Helper для обновления сообщения модерации ---

async def edit_message_text_or_caption(
    bot: Bot,
    message: Message,
    new_text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Редактирует text или caption в зависимости от типа сообщения."""
    chat_id = message.chat.id
    message_id = message.message_id
    try:
        if message.photo or message.video or message.voice or message.audio or message.animation:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=new_text,
                reply_markup=reply_markup,
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=new_text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
    except TelegramBadRequest as exc:
        if "exactly the same" in str(exc).lower() or "not modified" in str(exc).lower():
            return
        raise


# ===========================================================================
# ХЕНДЛЕРЫ — пользовательские
# ===========================================================================

user_router = Router(name="user")


class HasAlbum(Filter):
    """Срабатывает, если в data есть album (от AlbumMiddleware)."""
    async def __call__(self, message: Message, album: Album | None = None) -> bool:
        return album is not None


async def _accept_submission(
    message: Message,
    *,
    content_type: str,
    caption: str,
    extra_data: dict[str, Any],
) -> None:
    """Создаёт submission и отправляет его на модерацию.

    Если автор сам модератор — НЕ дублируем пост в его ЛС (он уже видел свой
    пост), а отправляем inline-кнопки как reply к исходному сообщению.
    Другим модераторам пост улетает в ЛС как обычно.

    Если автор не модератор — отправляем пост всем модераторам в ЛС
    + отвечаем «✅ Твой пост отправлен на модерацию».
    """
    user = message.from_user
    if not user:
        return
    await save_user(
        user.id, user.username or "", user.first_name or "", user.last_name or ""
    )

    if await is_user_banned(user.id):
        await message.answer("⛔ Ты забанен модератором. Публикации недоступны.")
        return

    author_name = (user.first_name or "") + (f" {user.last_name}" if user.last_name else "")
    submission_id = await create_submission(
        author_id=user.id,
        author_username=user.username or "",
        author_name=author_name,
        content_type=content_type,
        caption=caption,
        extra_data=extra_data,
    )
    submission = await get_submission(submission_id)
    if not submission:
        await message.answer("⚠ Не удалось сохранить пост. Попробуй ещё раз.")
        return

    bot = message.bot
    assert bot is not None

    # Если автор сам модератор — НЕ дублируем пост ему в ЛС (он уже видел свой
    # пост). Вместо этого отправляем inline-кнопки как reply к его сообщению.
    # В чате останется только 2 сообщения: исходный пост + reply с кнопками.
    if await is_moderator(user.id):
        try:
            reply_msg = await message.reply(
                f"📋 Пост #{submission_id} на модерации",
                reply_markup=moderation_kb(submission_id),
            )
            await add_submission_message(
                submission_id, user.id, user.id, reply_msg.message_id
            )
        except TelegramAPIError as exc:
            log.exception("Не удалось отправить reply с кнопками: %s", exc)
            await message.answer("⚠ Не удалось отправить пост на модерацию.")
            return

        # Также отправляем пост ДРУГИМ модераторам в ЛС (если они есть).
        # Автора пропускаем — он уже видит свой пост и кнопки.
        await send_to_moderation(bot, submission, skip_user_id=user.id)
        # НЕ отправляем «✅ Твой пост отправлен на модерацию» — reply с кнопками
        # уже подтвердил приём поста.
        return

    # Автор не модератор — отправляем пост всем модераторам в ЛС
    sent_count = 0
    try:
        sent_count = await send_to_moderation(bot, submission)
    except Exception as exc:
        log.exception("send_to_moderation failed: %s", exc)
        await message.answer(
            f"⚠ Не удалось отправить пост на модерацию: {exc}. "
            "Сообщи об этом администратору."
        )
        return

    if sent_count == 0:
        await message.answer(
            "✅ Твой пост поставлен в очередь на модерацию. "
            "Модератор проверит его, как только появится."
        )
        return

    await message.answer("✅ Твой пост отправлен на модерацию")


# --- Команды ---

@user_router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Отправь свой пост (текст, фото, видео, кружок, "
        "голосовое, GIF и т.д.), и он будет отправлен на модерацию. "
        "Я сообщу, когда его проверят."
    )


@user_router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📖 Помощь\n\n"
        "1. Пришли любой контент: текст / фото / альбом / видео / кружок / войс / GIF.\n"
        "2. Пост попадает в ЛС модераторам.\n"
        "3. Модератор решает: опубликовать анонимно в канале или отклонить.\n"
        "4. Тебе придёт уведомление о решении.\n\n"
        "В канале твоё имя и ID НЕ отображаются — посты полностью анонимны."
    )


@user_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🚫 Действие отменено. Пришли новый контент.")


# --- Альбом (первый, чтобы перехватывать до photo/video) ---

@user_router.message(HasAlbum())
async def handle_album(message: Message, album: Album) -> None:
    items = album.items
    if not items:
        return
    caption = album.caption.strip()
    await _accept_submission(
        message,
        content_type="album",
        caption=caption,
        extra_data={"items": items},
    )


# --- Текст ---

@user_router.message(F.text)
async def handle_text(message: Message) -> None:
    if not message.text:
        return
    if message.text.startswith("/"):
        await message.answer("ℹ Неизвестная команда. /help — список команд.")
        return
    text = message.text.strip()
    if not text:
        await message.answer("Пустой текст не могу принять. Попробуй ещё раз.")
        return
    if len(text) > 4096:
        await message.answer("Текст слишком длинный (макс. 4096 символов).")
        return
    await _accept_submission(
        message, content_type="text", caption=text, extra_data={},
    )


# --- Медиа ---

@user_router.message(F.photo)
async def handle_photo(message: Message) -> None:
    file_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()
    await _accept_submission(
        message, content_type="photo", caption=caption,
        extra_data={"file_id": file_id},
    )


@user_router.message(F.video)
async def handle_video(message: Message) -> None:
    file_id = message.video.file_id
    caption = (message.caption or "").strip()
    await _accept_submission(
        message, content_type="video", caption=caption,
        extra_data={"file_id": file_id},
    )


@user_router.message(F.video_note)
async def handle_video_note(message: Message) -> None:
    file_id = message.video_note.file_id
    await _accept_submission(
        message, content_type="video_note", caption="",
        extra_data={"file_id": file_id},
    )


@user_router.message(F.voice)
async def handle_voice(message: Message) -> None:
    file_id = message.voice.file_id
    caption = (message.caption or "").strip()
    await _accept_submission(
        message, content_type="voice", caption=caption,
        extra_data={"file_id": file_id},
    )


@user_router.message(F.audio)
async def handle_audio(message: Message) -> None:
    file_id = message.audio.file_id
    caption = (message.caption or "").strip()
    await _accept_submission(
        message, content_type="audio", caption=caption,
        extra_data={"file_id": file_id},
    )


@user_router.message(F.animation)
async def handle_animation(message: Message) -> None:
    file_id = message.animation.file_id
    caption = (message.caption or "").strip()
    await _accept_submission(
        message, content_type="animation", caption=caption,
        extra_data={"file_id": file_id},
    )


# --- Fallback ---

@user_router.message()
async def handle_unsupported(message: Message) -> None:
    await message.answer(
        "ℹ Этот тип контента не поддерживается. Я принимаю: текст, фото, альбом, "
        "видео, кружок, голосовое, аудио, GIF. /help — подробнее."
    )


# ===========================================================================
# ХЕНДЛЕРЫ — модераторские
# ===========================================================================

moderator_router = Router(name="moderator")


async def _ensure_moderator(cb: CallbackQuery) -> bool:
    if await is_moderator(cb.from_user.id):
        return True
    await cb.answer("⛔ Только модераторы могут это делать", show_alert=True)
    return False


@moderator_router.message(Command("pending"))
async def cmd_pending(message: Message) -> None:
    if not await is_moderator(message.from_user.id):
        return
    pending = await list_pending(limit=20)
    if not pending:
        await message.answer("✅ Очередь пуста. Все посты проверены.")
        return
    lines = []
    for s in pending:
        cap = (s["caption"] or "")[:60]
        lines.append(
            f"#{s['id']} [{s['content_type']}] — {cap or '(без подписи)'}"
        )
    await message.answer("📋 В очереди на модерацию:\n\n" + "\n".join(lines))


@moderator_router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not await is_moderator(message.from_user.id):
        return
    stats = await get_global_stats()
    mods = await get_all_moderator_ids()
    await message.answer(
        "📊 Статистика\n\n"
        f"Всего постов: {stats.get('total', 0)}\n"
        f"В очереди: {stats.get('pending', 0)}\n"
        f"Опубликовано: {stats.get('published', 0)}\n"
        f"Отклонено: {stats.get('rejected', 0)}\n\n"
        f"Модераторов: {len(mods)}",
    )


@moderator_router.callback_query(F.data.startswith("approve:"))
async def on_approve(cb: CallbackQuery, bot: Bot) -> None:
    if not await _ensure_moderator(cb):
        return
    submission_id = int(cb.data.split(":", 1)[1])
    submission = await get_submission(submission_id)
    if not submission:
        await cb.answer("Пост не найден", show_alert=True)
        return
    if submission["status"] != "pending":
        await cb.answer("Уже обработан", show_alert=True)
        return

    try:
        msg = await publish_submission(bot, submission)
    except TelegramAPIError as exc:
        log.exception("publish failed: %s", exc)
        await cb.answer(f"Ошибка публикации: {exc}", show_alert=True)
        return

    if not msg:
        await cb.answer("Не удалось опубликовать", show_alert=True)
        return

    await update_status(
        submission_id, "published",
        moderator_id=cb.from_user.id,
        published_msg_id=msg.message_id,
    )
    await increment_user_counters(submission["author_id"], published=True)
    await log_action(cb.from_user.id, "publish", submission_id)

    # Снимаем кнопки у всех остальных модераторов (это надо в любом случае)
    await mark_other_messages_done(
        bot, submission_id,
        skip_chat_id=cb.message.chat.id,
        skip_message_id=cb.message.message_id,
    )

    # Если автор сам и есть модератор (т.е. ты сам прислал и сам опубликовал) —
    # не присылать второе сообщение. Иначе будет два сообщения в одном чате:
    # «✅ ОПУБЛИКОВАН» (edit_caption) + «✅ Твой пост опубликован в канале!» (новое).
    # В этом случае сразу делаем edit_caption с текстом для автора.
    if submission["author_id"] == cb.from_user.id:
        await edit_message_text_or_caption(
            bot, cb.message, "✅ Твой пост опубликован в канале!", reply_markup=None
        )
    else:
        # Автор и модератор — разные люди: модератору — «✅ ОПУБЛИКОВАН»,
        # автору — отдельное уведомление.
        await edit_message_text_or_caption(
            bot, cb.message, "✅ ОПУБЛИКОВАН", reply_markup=None
        )
        try:
            await bot.send_message(
                submission["author_id"],
                "✅ Твой пост опубликован в канале!",
            )
        except TelegramAPIError:
            pass

    await cb.answer("Опубликовано")


@moderator_router.callback_query(F.data.startswith("reject:"))
async def on_reject(cb: CallbackQuery, bot: Bot) -> None:
    """Сразу отклоняет пост без выбора причины."""
    if not await _ensure_moderator(cb):
        return
    submission_id = int(cb.data.split(":", 1)[1])
    submission = await get_submission(submission_id)
    if not submission:
        await cb.answer("Пост не найден", show_alert=True)
        return
    if submission["status"] != "pending":
        await cb.answer("Уже обработан", show_alert=True)
        return

    await update_status(
        submission_id, "rejected", moderator_id=cb.from_user.id,
    )
    await increment_user_counters(submission["author_id"], rejected=True)
    await log_action(cb.from_user.id, "reject", submission_id)

    # Снимаем кнопки у всех остальных модераторов
    await mark_other_messages_done(
        bot, submission_id,
        skip_chat_id=cb.message.chat.id,
        skip_message_id=cb.message.message_id,
    )

    reject_text_for_author = (
        "❌ Твой пост отклонён модератором.\n\n"
        "Ты можешь отредактировать пост и прислать снова."
    )

    # Если автор сам и есть модератор — не присылать второе сообщение,
    # сразу делаем edit_caption с текстом для автора.
    if submission["author_id"] == cb.from_user.id:
        await edit_message_text_or_caption(
            bot, cb.message, reject_text_for_author, reply_markup=None
        )
    else:
        await edit_message_text_or_caption(
            bot, cb.message, "❌ ОТКЛОНЁН", reply_markup=None
        )
        try:
            await bot.send_message(submission["author_id"], reject_text_for_author)
        except TelegramAPIError:
            pass

    await cb.answer("Отклонено")


@moderator_router.callback_query(F.data.startswith("profile:"))
async def on_profile(cb: CallbackQuery) -> None:
    """Показывает профиль автора — только по запросу модератора."""
    if not await _ensure_moderator(cb):
        return
    submission_id = int(cb.data.split(":", 1)[1])
    submission = await get_submission(submission_id)
    if not submission:
        await cb.answer("Пост не найден", show_alert=True)
        return
    user_stats = await get_author_stats(submission["author_id"])
    user_info = await get_user(submission["author_id"])
    banned = "✅ забанен" if user_info and user_info["banned"] else "нет"
    text = (
        f"👤 Профиль автора поста #{submission_id}\n\n"
        f"ID: <code>{submission['author_id']}</code>\n"
        f"Имя: {submission['author_name'] or '—'}\n"
        f"Забанен: {banned}\n\n"
        f"Статистика по постам:\n"
        f"• Опубликовано: {user_stats.get('published', 0)}\n"
        f"• Отклонено: {user_stats.get('rejected', 0)}\n"
        f"• В очереди: {user_stats.get('pending', 0)}\n"
    )
    await cb.message.answer(text, parse_mode=ParseMode.HTML)
    await cb.answer()


# ===========================================================================
# ХЕНДЛЕРЫ — админ-панель
# ===========================================================================

admin_router = Router(name="admin")


async def _ensure_admin(message_or_cb: Message | CallbackQuery) -> bool:
    user_id = message_or_cb.from_user.id
    if is_super_admin(user_id):
        return True
    if isinstance(message_or_cb, Message):
        await message_or_cb.answer("⛔ Только супер-админ может это делать")
    else:
        await message_or_cb.answer("⛔ Только супер-админ может это делать", show_alert=True)
    return False


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    await message.answer("🛠 Админ-панель:", reply_markup=admin_kb())


@admin_router.message(Command("addmod"))
async def cmd_addmod(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) == 2:
        target = args[1].strip()
        if target.startswith("@"):
            await message.answer(
                "ℹ Для добавления модератора нужен числовой ID пользователя.\n"
                "Узнать ID можно через @userinfobot."
            )
            return
        try:
            uid = int(target)
        except ValueError:
            await message.answer("Некорректный ID. Используй: /addmod 123456789")
            return
        await add_moderator(uid, "", "", message.from_user.id)
        await message.answer(f"✅ Модератор с ID <code>{uid}</code> добавлен.")
        return
    await state.set_state(AddModeratorForm.waiting_user_id)
    await message.answer("Пришли числовой ID нового модератора:", reply_markup=cancel_kb())


@admin_router.message(AddModeratorForm.waiting_user_id)
async def on_addmod_input(message: Message, state: FSMContext) -> None:
    if not is_super_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    try:
        uid = int(raw)
    except ValueError:
        await message.answer("Некорректный ID. Введи число:")
        return
    await add_moderator(uid, "", "", message.from_user.id)
    await state.clear()
    await message.answer(f"✅ Модератор с ID <code>{uid}</code> добавлен.")


@admin_router.message(Command("delmod"))
async def cmd_delmod(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Использование: /delmod <id>")
        return
    try:
        uid = int(args[1].strip())
    except ValueError:
        await message.answer("Некорректный ID. Введи число.")
        return
    removed = await remove_moderator(uid)
    if removed:
        await message.answer(f"✅ Модератор с ID <code>{uid}</code> удалён.")
    else:
        await message.answer(f"ℹ Модератор с ID {uid} не найден в таблице (БД).")


@admin_router.message(Command("listmod"))
async def cmd_listmod(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    db_mods = await list_moderators_db()
    if not db_mods:
        await message.answer(
            "ℹ Список модераторов (через /addmod) пуст.\n"
            "Добавь через /addmod <id> — узнай ID через @userinfobot."
        )
        return

    lines = ["📋 Модераторы:"]
    lines.append("\n📌 SUPER_ADMINS (тоже модераторы, заданы в коде):")
    for uid in SUPER_ADMINS:
        lines.append(f"  • <code>{uid}</code>")
    lines.append("\n📌 Из БД (через /addmod):")
    if db_mods:
        for m in db_mods:
            lines.append(
                f"  • <code>{m['user_id']}</code> @{m['username'] or 'нет'} — {m['first_name'] or '—'}"
            )
    else:
        lines.append("  (пусто)")

    await message.answer("\n".join(lines))


# --- Inline callbacks админ-панели ---

@admin_router.callback_query(F.data == "admin:addmod")
async def admin_addmod_cb(cb: CallbackQuery, state: FSMContext) -> None:
    if not await _ensure_admin(cb):
        return
    await state.set_state(AddModeratorForm.waiting_user_id)
    await cb.message.answer("Пришли числовой ID нового модератора:", reply_markup=cancel_kb())
    await cb.answer()


@admin_router.callback_query(F.data == "admin:listmod")
async def admin_listmod_cb(cb: CallbackQuery) -> None:
    if not await _ensure_admin(cb):
        return
    db_mods = await list_moderators_db()
    lines = ["📋 Модераторы:"]
    lines.append("\n📌 SUPER_ADMINS (заданы в коде):")
    for uid in SUPER_ADMINS:
        lines.append(f"  • <code>{uid}</code>")
    lines.append("\n📌 Добавлены через /addmod (БД):")
    if db_mods:
        for m in db_mods:
            lines.append(
                f"  • <code>{m['user_id']}</code> @{m['username'] or 'нет'} — {m['first_name'] or '—'}"
            )
    else:
        lines.append("  (пусто)")
    await cb.message.answer("\n".join(lines))
    await cb.answer()


@admin_router.callback_query(F.data == "admin:stats")
async def admin_stats_cb(cb: CallbackQuery) -> None:
    if not await _ensure_admin(cb):
        return
    stats = await get_global_stats()
    await cb.message.answer(
        "📊 Статистика:\n\n"
        f"Всего постов: {stats.get('total', 0)}\n"
        f"В очереди: {stats.get('pending', 0)}\n"
        f"Опубликовано: {stats.get('published', 0)}\n"
        f"Отклонено: {stats.get('rejected', 0)}"
    )
    await cb.answer()


@admin_router.callback_query(F.data == "admin:cancel")
async def admin_cancel_cb(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await cb.message.edit_text("🚫 Отменено.")
    except TelegramBadRequest:
        await cb.message.answer("🚫 Отменено.")
    await cb.answer()


# ===========================================================================
# ТОЧКА ВХОДА
# ===========================================================================


def build_dispatcher() -> Dispatcher:
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Порядок роутеров важен: admin > moderator > user
    dp.include_router(admin_router)
    dp.include_router(moderator_router)
    dp.include_router(user_router)

    # AlbumMiddleware на outer Message-уровне (ловит раньше хендлеров)
    dp.message.outer_middleware(AlbumMiddleware())
    return dp


async def run_polling() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()

    # Early-check: проверяем токен ДО запуска polling'а.
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        log.error("=" * 70)
        log.error("❌ НЕВЕРНЫЙ BOT_TOKEN — Telegram отклонил токен.")
        log.error("Что делать:")
        log.error("  1) Открой @BotFather в Telegram")
        log.error("  2) /newbot → придумай имя и username бота")
        log.error("  3) Скопируй токен вида 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx")
        log.error("  4) Открой bot.py в редакторе (Блокнот/VSCode/PyCharm)")
        log.error("  5) Найди в начале файла строку BOT_TOKEN = ...")
        log.error("  6) Замени плейсхолдер на свой реальный токен")
        log.error("  7) Сохрани файл и запусти заново: python bot.py")
        log.error("=" * 70)
        await bot.session.close()
        return
    except TelegramForbiddenError:
        log.error("❌ Бот забанен Telegram. Создай нового бота через @BotFather.")
        await bot.session.close()
        return
    except TelegramAPIError as exc:
        log.error("❌ Не удалось подключиться к Telegram API: %s", exc)
        log.error("Проверь интернет-соединение и корректность BOT_TOKEN.")
        await bot.session.close()
        return

    log.info("Бот @%s запущен в polling-режиме", me.username)
    log.info("Канал публикации: %s", CHANNEL_ID)
    log.info("Супер-админы: %s", SUPER_ADMINS)
    log.info("Модераторы добавляются через /addmod (хранятся в БД)")

    # Early-check канала публикации
    try:
        chat = await bot.get_chat(CHANNEL_ID)
        log.info("✓ Доступ к каналу подтверждён: %s (id=%s)", chat.title, chat.id)
    except TelegramBadRequest as exc:
        log.error("=" * 70)
        log.error("❌ Бот не имеет доступа к каналу %s: %s", CHANNEL_ID, exc)
        log.error("Что проверить:")
        log.error("  1) CHANNEL_ID указан правильно?")
        log.error("     Узнать ID: переслать любое сообщение из канала в @userinfobot")
        log.error("  2) Бот добавлен в АДМИНИСТРАТОРЫ канала?")
        log.error("     В канале: Settings → Administrators → Add admin → @твой_бот")
        log.error("  3) У бота есть право «Post Messages» (Publish posts)?")
        log.error("=" * 70)
        await bot.session.close()
        return

    # Проверяем, что бот — админ канала с правом публикации
    try:
        member = await bot.get_chat_member(CHANNEL_ID, me.id)
        if member.status not in ("administrator", "creator"):
            log.error("=" * 70)
            log.error("❌ Бот НЕ админ канала %s (статус: %s)", CHANNEL_ID, member.status)
            log.error("Добавь бота в администраторы канала, иначе публикация не сработает.")
            log.error("В канале: Settings → Administrators → Add admin → @твой_бот")
            log.error("=" * 70)
            await bot.session.close()
            return
        log.info("✓ Бот — админ канала (статус: %s)", member.status)
    except TelegramAPIError as exc:
        log.warning("Не удалось проверить права бота в канале: %s", exc)

    # Проверяем модераторов: можно ли им отправить сообщение
    all_mods = await get_all_moderator_ids()
    bad_mods: list[int] = []
    for mod_id in all_mods:
        try:
            await bot.send_chat_action(chat_id=mod_id, action="typing")
        except TelegramBadRequest:
            bad_mods.append(mod_id)
        except TelegramAPIError:
            bad_mods.append(mod_id)
    if bad_mods:
        log.warning("=" * 70)
        log.warning("⚠ Этим модераторам бот НЕ МОЖЕТ отправить посты (пусть нажмут /start):")
        for mod_id in bad_mods:
            log.warning("  • %s", mod_id)
        log.warning("Возможные причины:")
        log.warning("  • Неверный ID (например, заглушка)")
        log.warning("  • Модератор никогда не открывал бота (/start)")
        log.warning("  • Модератор заблокировал бота")
        log.warning("Добавь модераторов через /addmod <id> (только супер-админ)")
        log.warning("=" * 70)
    else:
        log.info("✓ Все модераторы доступны (%d чел.)", len(all_mods))

    # Сбрасываем старый webhook, если он был установлен ранее
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramAPIError as exc:
        log.warning("Не удалось сбросить webhook: %s", exc)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


async def run_webhook() -> None:
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()
    me = await bot.get_me()
    log.info("Бот @%s запущен в webhook-режиме", me.username)
    log.info("Webhook URL: %s", WEBHOOK_URL)

    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register_request_handler(app, path="/")
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=WEBHOOK_PORT)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot.session.close()


async def main() -> None:
    # Проверяем конфигурацию ДО инициализации БД и подключения к Telegram
    errors = _validate_config()
    if errors:
        log.error("=" * 70)
        log.error("❌ ОШИБКА КОНФИГУРАЦИИ — бот не запущен.\n")
        for e in errors:
            log.error(e + "\n")
        log.error("Что делать:")
        log.error("  1) Открой bot.py в редакторе (Блокнот / VSCode / PyCharm)")
        log.error("  2) Найди в самом начале файла раздел «НАСТРОЙКИ БОТА»")
        log.error("  3) Замени плейсхолдеры на свои реальные значения")
        log.error("  4) Сохрани файл и запусти заново: python bot.py")
        log.error("=" * 70)
        return

    await init_db()
    if WEBHOOK_URL:
        await run_webhook()
    else:
        await run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")
    except TokenValidationError as exc:
        log.error("Некорректный токен: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.exception("Фатальная ошибка: %s", exc)
        sys.exit(1)
