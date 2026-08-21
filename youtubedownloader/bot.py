import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup, Message

MAX_FILE_SIZE = 50 * 1024 * 1024
REQUEST_TTL_SECONDS = 30 * 60
MAX_CONCURRENT_DOWNLOADS = 2
VIDEO_QUALITIES = (360, 480, 720, 1080)
AUDIO_BITRATES = (128, 192)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

dp = Dispatcher()
download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
active_users: set[int] = set()
active_users_lock = asyncio.Lock()


class DownloadError(Exception):
    """Понятная пользователю ошибка получения или загрузки ролика."""


@dataclass(frozen=True)
class DownloadRequest:
    owner_id: int
    url: str
    title: str
    uploader: str
    duration: int | None
    video_qualities: tuple[int, ...]
    audio_bitrates: tuple[int, ...]
    created_at: float


requests: dict[str, DownloadRequest] = {}


def human_size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} МБ"


def normalize_youtube_url(text: str) -> str | None:
    value = text.strip()
    if not re.fullmatch(r"https?://\S+", value, flags=re.IGNORECASE):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS or parsed.scheme.lower() not in {"http", "https"}:
        return None
    query = parse_qs(parsed.query)
    if "list" in query:
        return None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = query.get("v", [""])[0]
    elif parsed.path.startswith("/shorts/"):
        parts = parsed.path.strip("/").split("/")
        video_id = parts[1] if len(parts) == 2 else ""
    else:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def format_size(item: dict[str, Any]) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = item.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    duration = item.get("duration")
    bitrate = item.get("tbr")
    if isinstance(duration, (int, float)) and isinstance(bitrate, (int, float)):
        return int(duration * bitrate * 1000 / 8 * 1.08)
    return None


def available_video_qualities(info: dict[str, Any]) -> tuple[int, ...]:
    formats = info.get("formats") or []
    audio_sizes = [
        size
        for item in formats
        if item.get("acodec") not in (None, "none")
        and (size := format_size(item)) is not None
    ]
    smallest_audio_size = min(audio_sizes) if audio_sizes else 0
    result: list[int] = []
    for quality in VIDEO_QUALITIES:
        candidates: list[int | None] = []
        for item in formats:
            height = item.get("height")
            if height != quality or item.get("vcodec") in (None, "none"):
                continue
            size = format_size(item)
            if size is not None and item.get("acodec") in (None, "none"):
                size += smallest_audio_size
            candidates.append(size)
        if candidates:
            known_sizes = [size for size in candidates if size is not None]
            if not known_sizes or min(known_sizes) <= MAX_FILE_SIZE:
                result.append(quality)
    return tuple(result)


def available_audio_bitrates(info: dict[str, Any]) -> tuple[int, ...]:
    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return AUDIO_BITRATES
    return tuple(
        bitrate
        for bitrate in AUDIO_BITRATES
        if duration * bitrate * 1000 / 8 * 1.08 <= MAX_FILE_SIZE
    )


def extract_metadata_sync(url: str) -> dict[str, Any]:
    options = {"quiet": True, "no_warnings": True, "noplaylist": True,
               "skip_download": True, "socket_timeout": 20, "retries": 2}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as error:
        raise DownloadError(
            "Не удалось получить видео. Возможно, оно удалено, приватное, "
            "имеет возрастное ограничение или временно недоступно."
        ) from error
    if not isinstance(info, dict) or info.get("_type") == "playlist":
        raise DownloadError("Плейлисты не поддерживаются. Отправьте ссылку на одно видео.")
    return info


def cleanup_requests() -> None:
    cutoff = time.monotonic() - REQUEST_TTL_SECONDS
    for key in [key for key, value in requests.items() if value.created_at < cutoff]:
        requests.pop(key, None)


def request_for_callback(callback: CallbackQuery, request_id: str) -> DownloadRequest | None:
    cleanup_requests()
    request = requests.get(request_id)
    if request is None or request.owner_id != callback.from_user.id:
        return None
    return request


def format_keyboard(request_id: str, request: DownloadRequest) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if request.video_qualities:
        rows.append([InlineKeyboardButton(text="🎬 Видео MP4",
                                          callback_data=f"format:{request_id}:v")])
    if request.audio_bitrates:
        rows.append([InlineKeyboardButton(text="🎵 Аудио MP3",
                                          callback_data=f"format:{request_id}:a")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quality_keyboard(request_id: str, kind: Literal["v", "a"],
                     request: DownloadRequest) -> InlineKeyboardMarkup:
    values = request.video_qualities if kind == "v" else request.audio_bitrates
    suffix = "p" if kind == "v" else " кбит/с"
    rows = [[InlineKeyboardButton(text=f"{value}{suffix}",
              callback_data=f"download:{request_id}:{kind}:{value}")] for value in values]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"back:{request_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def download_sync(url: str, kind: Literal["v", "a"], quality: int,
                  directory: Path) -> Path:
    common: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
        "outtmpl": str(directory / "%(id)s.%(ext)s"), "windowsfilenames": True,
    }
    if kind == "v":
        common.update({
            "format": (f"bestvideo[height={quality}][ext=mp4]+bestaudio[ext=m4a]/"
                       f"best[height={quality}][ext=mp4]/best[height={quality}]"),
            "merge_output_format": "mp4",
        })
    else:
        common.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                "preferredquality": str(quality)}],
        })
    try:
        with yt_dlp.YoutubeDL(common) as ydl:
            ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as error:
        raise DownloadError(
            "Не удалось скачать выбранный формат. Попробуйте другое качество позже."
        ) from error
    files = [path for path in directory.iterdir()
             if path.is_file() and path.suffix not in {".part", ".ytdl", ".temp"}]
    if not files:
        raise DownloadError("Загрузка завершилась без итогового файла.")
    return max(files, key=lambda path: path.stat().st_size)


async def reserve_user(user_id: int) -> bool:
    async with active_users_lock:
        if user_id in active_users:
            return False
        active_users.add(user_id)
        return True


async def release_user(user_id: int) -> None:
    async with active_users_lock:
        active_users.discard(user_id)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Здравствуйте! Отправьте мне ссылку на одиночное видео YouTube или Shorts. "
        "Я предложу скачать видео MP4 или аудио MP3.\n\n"
        "Используйте бот только для контента, который вы имеете право скачивать."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Поддерживаются ссылки youtube.com/watch, youtu.be и youtube.com/shorts.\n"
        "Видео: 360p, 480p, 720p или 1080p. Аудио: MP3 128 или 192 кбит/с.\n"
        "Максимальный итоговый файл — 50 МБ. Плейлисты, приватные и защищённые "
        "видео не поддерживаются."
    )


@dp.message(F.text)
async def receive_url(message: Message) -> None:
    if message.from_user is None or message.text is None:
        return
    url = normalize_youtube_url(message.text)
    if url is None:
        await message.answer("Отправьте одну корректную ссылку на видео YouTube или Shorts. "
                             "Плейлисты не поддерживаются. Справка: /help.")
        return
    status = await message.answer("🔎 Получаю информацию о видео…")
    try:
        info = await asyncio.to_thread(extract_metadata_sync, url)
        video_qualities = available_video_qualities(info)
        audio_bitrates = available_audio_bitrates(info)
        if not video_qualities and not audio_bitrates:
            raise DownloadError("Доступные варианты превышают лимит 50 МБ.")
        request_id = uuid.uuid4().hex[:12]
        request = DownloadRequest(
            owner_id=message.from_user.id, url=url,
            title=str(info.get("title") or "Видео")[:200],
            uploader=str(info.get("uploader") or info.get("channel") or "")[:100],
            duration=int(info["duration"]) if isinstance(info.get("duration"), (int, float)) else None,
            video_qualities=video_qualities, audio_bitrates=audio_bitrates,
            created_at=time.monotonic(),
        )
        cleanup_requests()
        requests[request_id] = request
        await status.edit_text(f"«{request.title}»\nВыберите формат:",
                               reply_markup=format_keyboard(request_id, request))
    except DownloadError as error:
        await status.edit_text(str(error))
    except Exception:
        await status.edit_text("Неожиданная ошибка при получении видео. Попробуйте позже.")


@dp.callback_query(F.data.startswith("format:"))
async def choose_format(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    _, request_id, kind = callback.data.split(":", 2)
    request = request_for_callback(callback, request_id)
    if request is None or kind not in {"v", "a"}:
        await callback.answer("Этот запрос устарел. Отправьте ссылку заново.", show_alert=True)
        return
    await callback.answer()
    label = "качество видео" if kind == "v" else "битрейт аудио"
    await callback.message.edit_text(f"«{request.title}»\nВыберите {label}:",
        reply_markup=quality_keyboard(request_id, kind, request))


@dp.callback_query(F.data.startswith("back:"))
async def go_back(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    request_id = callback.data.split(":", 1)[1]
    request = request_for_callback(callback, request_id)
    if request is None:
        await callback.answer("Этот запрос устарел. Отправьте ссылку заново.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(f"«{request.title}»\nВыберите формат:",
        reply_markup=format_keyboard(request_id, request))


@dp.callback_query(F.data.startswith("download:"))
async def download_callback(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    try:
        _, request_id, raw_kind, raw_quality = callback.data.split(":", 3)
        kind: Literal["v", "a"] = "v" if raw_kind == "v" else "a"
        quality = int(raw_quality)
    except (ValueError, TypeError):
        await callback.answer("Некорректный вариант загрузки.", show_alert=True)
        return
    request = request_for_callback(callback, request_id)
    allowed = (request.video_qualities if request and kind == "v"
               else request.audio_bitrates if request else ())
    if request is None or raw_kind not in {"v", "a"} or quality not in allowed:
        await callback.answer("Этот запрос устарел. Отправьте ссылку заново.", show_alert=True)
        return
    user_id = callback.from_user.id
    if not await reserve_user(user_id):
        await callback.answer("У вас уже выполняется загрузка.", show_alert=True)
        return
    await callback.answer("Загрузка началась")
    await callback.message.edit_text("⏳ Ожидаю свободное место в очереди…")
    temp_path: str | None = None
    try:
        async with download_slots:
            await callback.message.edit_text("⬇️ Скачиваю и обрабатываю файл…")
            temp_path = tempfile.mkdtemp(prefix="youtube_bot_")
            output = await asyncio.to_thread(download_sync, request.url, kind, quality,
                                             Path(temp_path))
            actual_size = output.stat().st_size
            if actual_size > MAX_FILE_SIZE:
                raise DownloadError(f"Итоговый файл занимает {human_size(actual_size)} и "
                                    "превышает лимит 50 МБ. Выберите меньшее качество.")
            await callback.message.edit_text("📤 Отправляю файл в Telegram…")
            media = FSInputFile(output, filename=output.name)
            if kind == "v":
                await callback.message.answer_video(media, caption=request.title,
                    supports_streaming=True, request_timeout=180)
            else:
                await callback.message.answer_audio(media, caption=request.title,
                    title=request.title, performer=request.uploader or None,
                    request_timeout=180)
            await callback.message.edit_text("✅ Готово. Можете отправить следующую ссылку.")
            requests.pop(request_id, None)
    except DownloadError as error:
        await callback.message.edit_text(f"❌ {error}")
    except TelegramAPIError:
        await callback.message.edit_text("❌ Telegram не смог принять файл. Попробуйте "
                                         "меньшее качество или повторите позже.")
    except Exception:
        await callback.message.edit_text("❌ Неожиданная ошибка загрузки. Попробуйте позже.")
    finally:
        if temp_path is not None:
            shutil.rmtree(temp_path, ignore_errors=True)
        await release_user(user_id)


@dp.message()
async def unknown_message(message: Message) -> None:
    await message.answer("Отправьте текстовую ссылку на видео YouTube. Справка: /help")


def get_bot_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан токен Telegram-бота. В PowerShell выполните: "
                           "$env:BOT_TOKEN='НОВЫЙ_ТОКЕН'")
    return token


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("FFmpeg не найден в PATH. Установите FFmpeg для Windows "
                           "и перезапустите терминал.")


async def main() -> None:
    check_ffmpeg()
    bot = Bot(token=get_bot_token())
    print("Бот запущен. Для остановки нажмите Ctrl+C.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as error:
        print(f"Ошибка запуска: {error}", file=sys.stderr)
        raise SystemExit(1) from error
