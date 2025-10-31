# -*- coding: utf-8 -*-
"""
Liberty Country — мощный музыкальный модуль (Music Cog)

Особенности:
- Slash-команды в группе /music: join, leave, play, playtop, pause, resume, skip, stop,
  queue, remove, move, clear, loop, shuffle, volume, nowplaying, lyrics.
- Очередь с перемещением/удалением, режимы повтора: off/track/queue/auto.
- Плеер на FFmpeg + yt-dlp (YouTube ссылки и поиски ytsearch:).
- Безопасное автодополнение (тип параметра str/int, варианты — через Choice в autocomplete).
- Интеграция с веб-панелью: экспорт состояния в LC_STATE_FILE, чтение команд из LC_CONTROL_FILE.
- Windows-safe запись/чтение файлов (fallback в %TEMP% при ошибках прав).

Важно:
- НЕ используем аннотации Choice[...] в параметрах СЛЭШ-команд с autocomplete — это и было источником ошибки.
- Для фиксированных наборов — @app_commands.choices + обычный тип str/int.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands, Interaction
from discord.ext import commands

# --- yt-dlp (извлечение медиа)
try:
    import yt_dlp as youtube_dl
except Exception:  # pragma: no cover
    youtube_dl = None


# ==============================
# Конфигурация / константы
# ==============================

YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "extract_flat": False,
    "noplaylist": False,
    "source_address": "0.0.0.0",  # иногда помогает от сетевых проблем
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"

STATE_FILE = Path(os.getenv("LC_STATE_FILE", "lc_nowplaying.json")).resolve()
CONTROL_FILE = Path(os.getenv("LC_CONTROL_FILE", "control_queue.jsonl")).resolve()
QUEUE_EXPORT_LIMIT = int(os.getenv("LC_QUEUE_EXPORT_LIMIT", "100") or "100")

# ==============================
# Вспомогательные структуры
# ==============================

@dataclass
class Track:
    title: str
    url: str
    webpage_url: str
    duration: int  # секунды
    requester_id: int
    thumbnail: Optional[str] = None
    # служебные:
    added_at: float = field(default_factory=time.time)

    @property
    def display_title(self) -> str:
        return self.title or self.webpage_url or self.url

    def brief(self) -> dict:
        return {
            "title": self.display_title,
            "url": self.webpage_url or self.url,
            "duration": self.duration,
            "requester": self.requester_id,
            "thumb": self.thumbnail,
        }


class GuildPlayer:
    """Состояние плеера для одного сервера."""

    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: List[Track] = []
        self.volume: int = 100
        self.loop_mode: str = "off"  # off | track | queue | auto
        self.current: Optional[Track] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.lock = asyncio.Lock()
        self.next_event = asyncio.Event()
        self._play_task: Optional[asyncio.Task] = None
        self._vc_ready = asyncio.Event()

    # ---- очередь / управление
    def q_len(self) -> int:
        return len(self.queue)

    def q_clear(self):
        self.queue.clear()

    def q_add(self, track: Track, to_front: bool = False):
        if to_front:
            self.queue.insert(0, track)
        else:
            self.queue.append(track)

    def q_move(self, src: int, dst: int) -> bool:
        """1-based индексы."""
        i, j = src - 1, dst - 1
        if i < 0 or j < 0 or i >= len(self.queue) or j >= len(self.queue):
            return False
        item = self.queue.pop(i)
        self.queue.insert(j, item)
        return True

    def q_remove(self, index: int) -> Optional[Track]:
        i = index - 1
        if 0 <= i < len(self.queue):
            return self.queue.pop(i)
        return None

    def q_shuffle(self):
        random.shuffle(self.queue)

    # ---- голос / плеер
    async def ensure_voice(self, interaction: Interaction, move: bool = False):
        if interaction.user is None:
            raise RuntimeError("No user on interaction.")
        if not isinstance(interaction.user, (discord.Member,)):
            raise RuntimeError("Not a guild interaction.")

        member: discord.Member = interaction.user
        if member.voice is None or member.voice.channel is None:
            raise app_commands.AppCommandError("Зайдите в голосовой канал.")

        channel = member.voice.channel
        if self.voice and self.voice.channel and self.voice.is_connected():
            if move and self.voice.channel.id != channel.id:
                await self.voice.move_to(channel)
        else:
            self.voice = await channel.connect()
        self._vc_ready.set()

    async def disconnect(self):
        self.queue.clear()
        self.current = None
        self.next_event.set()
        if self.voice and self.voice.is_connected():
            await self.voice.disconnect(force=True)
        self.voice = None
        self._vc_ready.clear()

    async def start_player_loop(self):
        if self._play_task and not self._play_task.done():
            return
        self._play_task = asyncio.create_task(self._player_loop(), name=f"player-{self.guild_id}")

    async def _player_loop(self):
        """Основной цикл воспроизведения очереди."""
        while True:
            try:
                await self._vc_ready.wait()
                self.next_event.clear()

                # если ничего не играет — берём из очереди
                if self.current is None:
                    if not self.queue:
                        # нет треков — ждём новый и паузим цикл
                        await asyncio.sleep(0.5)
                        continue
                    self.current = self.queue.pop(0)

                # воспроизводим текущий трек
                await self._play_current_track()

                # ожидаем завершение
                await self.next_event.wait()

                # отработка режима повтора
                if self.loop_mode == "track" and self.current:
                    # ничего не делаем — current останется и переиграется снова
                    pass
                elif self.loop_mode == "queue" and self.current:
                    self.queue.append(self.current)
                    self.current = None
                else:
                    # off / auto
                    self.current = None

            except asyncio.CancelledError:
                break
            except Exception:
                # чтобы цикл не умирал
                await asyncio.sleep(1)

    async def _play_current_track(self):
        if not self.voice or not self.current:
            return
        track = self.current

        # извлечь stream url через yt-dlp
        src_url = await extract_audio_url(track.url or track.webpage_url)
        if not src_url:
            # пропускаем трек
            self.next_event.set()
            return

        def _after_play(err: Optional[Exception]):
            # коллбек вызывается не в event loop'е
            try:
                self.bot.loop.call_soon_threadsafe(self.next_event.set)
            except Exception:
                pass

        audio = discord.FFmpegPCMAudio(
            src_url,
            before_options=FFMPEG_BEFORE,
            options=FFMPEG_OPTS,
        )
        source = discord.PCMVolumeTransformer(audio, volume=self.volume / 100.0)
        self.voice.play(source, after=_after_play)

    def set_volume(self, vol: int):
        self.volume = max(1, min(200, vol))
        if self.voice and self.voice.source and isinstance(self.voice.source, discord.PCMVolumeTransformer):
            self.voice.source.volume = self.volume / 100.0


# ==============================
# Вспомогательные функции
# ==============================

def _safe_file(path: Path) -> Path:
    """Создать файл/папки с fallback на TEMP при проблемах прав."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return path
    except Exception:
        tmp = Path(os.getenv("TEMP", "/tmp")) / "liberty_country"
        tmp.mkdir(parents=True, exist_ok=True)
        fallback = tmp / path.name
        fallback.touch(exist_ok=True)
        return fallback


async def extract_audio_url(query: str) -> Optional[str]:
    """Получить прямой url аудио-потока через yt-dlp (в executor)."""
    if youtube_dl is None:
        return None

    loop = asyncio.get_running_loop()

    def _extract() -> Optional[str]:
        with youtube_dl.YoutubeDL(YTDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if info is None:
                return None
            if "entries" in info:  # плейлист или поиск
                # берём первый элемент
                info = info["entries"][0]
            url = info.get("url")
            if not url and "formats" in info and info["formats"]:
                # fallback к первому формату
                url = info["formats"][0].get("url")
            return url

    try:
        return await loop.run_in_executor(None, _extract)
    except Exception:
        return None


async def ytdl_search(query: str) -> List[Track]:
    """Поиск/извлечение одного или нескольких треков через yt-dlp."""
    results: List[Track] = []
    if youtube_dl is None:
        return results

    loop = asyncio.get_running_loop()

    def _extract_all() -> List[dict]:
        with youtube_dl.YoutubeDL(YTDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if info is None:
                return []
            if "entries" in info:
                return [e for e in info["entries"] if e]
            return [info]

    try:
        items = await loop.run_in_executor(None, _extract_all)
    except Exception:
        items = []

    for it in items:
        title = it.get("title") or "Untitled"
        url = it.get("webpage_url") or it.get("url") or query
        wurl = it.get("webpage_url") or url
        duration = int(it.get("duration") or it.get("abr") or 0) or 0
        thumb = it.get("thumbnail")
        results.append(Track(title=title, url=url, webpage_url=wurl, duration=duration, requester_id=0, thumbnail=thumb))

    return results


def export_state(players: Dict[int, GuildPlayer]):
    """Экспорт состояния плееров в JSON для панели."""
    path = _safe_file(STATE_FILE)

    data: Dict[str, dict] = {}
    for gid, gp in players.items():
        cur = gp.current
        queue_list = [t.brief() for t in gp.queue[:QUEUE_EXPORT_LIMIT]]
        data[str(gid)] = {
            "guild_id": gid,
            "title": cur.display_title if cur else None,
            "url": (cur.webpage_url or cur.url) if cur else None,
            "duration": cur.duration if cur else 0,
            "thumb": cur.thumbnail if cur else None,
            "volume": gp.volume,
            "loop": gp.loop_mode,
            "queue_len": gp.q_len(),
            "queue": queue_list,
            "ts": int(time.time()),
        }

    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ==============================
# Slash Group (module-level)
# ==============================

music_group = app_commands.Group(name="music", description="Музыкальные команды Liberty Country")


# ==============================
# Cog
# ==============================

class MusicPower(commands.Cog):
    """Музыка для Liberty Country."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: Dict[int, GuildPlayer] = {}
        self._export_task: Optional[asyncio.Task] = None
        self._control_task: Optional[asyncio.Task] = None
        self._control_pos = 0  # позиция чтения control_queue.jsonl

    # --------- сервисные
    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self.bot, guild_id)
        return self.players[guild_id]

    async def cog_load(self):
        # регистрируем группу /music
        self.bot.tree.add_command(music_group)
        # периодический экспорт состояния (каждые 3 сек)
        self._export_task = asyncio.create_task(self._export_loop(), name="music-export")
        # чтение внешней очереди команд (панель)
        self._control_task = asyncio.create_task(self._control_loop(), name="music-control")

    async def cog_unload(self):
        try:
            self.bot.tree.remove_command(music_group.name, type=discord.AppCommandType.chat_input)
        except Exception:
            pass
        if self._export_task:
            self._export_task.cancel()
        if self._control_task:
            self._control_task.cancel()

    async def _export_loop(self):
        while True:
            try:
                export_state(self.players)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(3)

    async def _control_loop(self):
        """Читает control_queue.jsonl и выполняет команды (от панели)."""
        ctrl = _safe_file(CONTROL_FILE)
        # при первом запуске начинаем читать с конца файла
        try:
            self._control_pos = ctrl.stat().st_size
        except Exception:
            self._control_pos = 0

        while True:
            try:
                await asyncio.sleep(1.0)
                ctrl = _safe_file(CONTROL_FILE)
                with ctrl.open("rb") as f:
                    f.seek(self._control_pos)
                    raw = f.read()
                    self._control_pos = f.tell()
                if not raw:
                    continue
                for line in raw.splitlines():
                    try:
                        evt = json.loads(line.decode("utf-8", "ignore"))
                        await self._apply_control(evt)
                    except Exception:
                        continue
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1.5)

    async def _apply_control(self, evt: dict):
        """Выполнить команду от панели."""
        action = (evt.get("action") or "").lower()
        payload = evt.get("payload") or {}
        gid = int(payload.get("guild_id") or 0)
        if not gid:
            return
        gp = self.get_player(gid)

        # Некоторые действия требуют взаимодействия; здесь мы делаем best-effort без Interaction.
        if action in {"pause", "resume", "skip", "stop", "shuffle", "leave", "clear"}:
            if action == "pause" and gp.voice and gp.voice.is_playing():
                gp.voice.pause()
            elif action == "resume" and gp.voice and gp.voice.is_paused():
                gp.voice.resume()
            elif action == "skip" and gp.voice:
                gp.voice.stop()
            elif action == "stop" and gp.voice:
                gp.queue.clear()
                gp.current = None
                gp.voice.stop()
            elif action == "shuffle":
                gp.q_shuffle()
            elif action == "leave":
                await gp.disconnect()
            elif action == "clear":
                gp.q_clear()
            return

        if action == "loop":
            mode = str(payload.get("mode") or "off")
            gp.loop_mode = mode
            return

        if action == "volume":
            try:
                level = int(payload.get("level") or 100)
            except Exception:
                level = 100
            gp.set_volume(level)
            return

        if action in {"play", "playtop"}:
            query = str(payload.get("query") or "").strip()
            if not query:
                return
            # создаём Track(и)
            items = await ytdl_search(query if query.startswith("http") else f"ytsearch:{query}")
            for t in items[:1]:  # добавляем только первый найденный
                t.requester_id = int(payload.get("user_id") or 0)
                gp.q_add(t, to_front=(action == "playtop"))
            # автозапуск
            await gp.start_player_loop()
            return

        if action == "queue_remove":
            idx = int(payload.get("index") or 0)
            gp.q_remove(idx)
            return

        if action == "queue_move":
            src = int(payload.get("src") or 0)
            dst = int(payload.get("dst") or 0)
            gp.q_move(src, dst)
            return

    # ===========================
    # Slash-команды /music
    # ===========================

    @music_group.command(name="join", description="Подключиться к голосовому каналу")
    async def cmd_join(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        await gp.ensure_voice(interaction, move=True)
        await interaction.response.send_message("✅ Подключился к голосовому.", ephemeral=True)
        await gp.start_player_loop()

    @music_group.command(name="leave", description="Отключиться от голосового канала")
    async def cmd_leave(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        await gp.disconnect()
        await interaction.response.send_message("👋 Отключился.", ephemeral=True)

    @music_group.command(name="play", description="Воспроизвести трек или поиск по запросу/ссылке")
    @app_commands.describe(query="Название, ссылка или запрос (ytsearch: поддерживается автоматически)")
    async def cmd_play(self, interaction: Interaction, query: str):
        await interaction.response.defer(thinking=True, ephemeral=False)
        gp = self.get_player(interaction.guild_id)
        await gp.ensure_voice(interaction, move=True)

        q = query.strip()
        if not q:
            await interaction.followup.send("Укажите запрос или ссылку.")
            return

        items = await ytdl_search(q if q.startswith("http") else f"ytsearch:{q}")
        if not items:
            await interaction.followup.send("Ничего не найдено.")
            return

        t = items[0]
        t.requester_id = interaction.user.id
        gp.q_add(t, to_front=False)
        await gp.start_player_loop()

        await interaction.followup.send(f"➕ Добавлено в очередь: **{t.display_title}**")

    @cmd_play.autocomplete("query")
    async def ac_play(self, interaction: Interaction, current: str) -> List[app_commands.Choice[str]]:
        # Заготовка (можно подключить кэш/историю запросов)
        s = (current or "").strip()
        presets = [s, f"{s} official", f"{s} lyrics", f"{s} remix"] if s else []
        return [app_commands.Choice(name=p, value=p) for p in presets if p][:25]

    @music_group.command(name="playtop", description="Поставить трек в начало очереди")
    @app_commands.describe(query="Название, ссылка или запрос")
    async def cmd_playtop(self, interaction: Interaction, query: str):
        await interaction.response.defer(thinking=True)
        gp = self.get_player(interaction.guild_id)
        await gp.ensure_voice(interaction, move=True)
        q = query.strip()
        items = await ytdl_search(q if q.startswith("http") else f"ytsearch:{q}")
        if not items:
            await interaction.followup.send("Ничего не найдено.")
            return
        t = items[0]
        t.requester_id = interaction.user.id
        gp.q_add(t, to_front=True)
        await gp.start_player_loop()
        await interaction.followup.send(f"⏫ В начало очереди: **{t.display_title}**")

    @cmd_playtop.autocomplete("query")
    async def ac_playtop(self, interaction: Interaction, current: str) -> List[app_commands.Choice[str]]:
        s = (current or "").strip()
        presets = [s, f"{s} fast", f"{s} 1 hour"] if s else []
        return [app_commands.Choice(name=p, value=p) for p in presets if p][:25]

    @music_group.command(name="pause", description="Пауза")
    async def cmd_pause(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        if gp.voice and gp.voice.is_playing():
            gp.voice.pause()
            await interaction.response.send_message("⏸ Пауза", ephemeral=True)
        else:
            await interaction.response.send_message("Нечего ставить на паузу.", ephemeral=True)

    @music_group.command(name="resume", description="Продолжить")
    async def cmd_resume(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        if gp.voice and gp.voice.is_paused():
            gp.voice.resume()
            await interaction.response.send_message("▶ Продолжил", ephemeral=True)
        else:
            await interaction.response.send_message("Нечего продолжать.", ephemeral=True)

    @music_group.command(name="skip", description="Пропустить текущий трек")
    async def cmd_skip(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        if gp.voice and (gp.voice.is_playing() or gp.voice.is_paused()):
            gp.voice.stop()
            await interaction.response.send_message("⏭ Пропустил", ephemeral=True)
        else:
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)

    @music_group.command(name="stop", description="Остановить и очистить очередь")
    async def cmd_stop(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        gp.queue.clear()
        gp.current = None
        if gp.voice:
            gp.voice.stop()
        await interaction.response.send_message("⏹ Остановлено, очередь очищена.", ephemeral=True)

    @music_group.command(name="queue", description="Показать очередь")
    async def cmd_queue(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        if not gp.current and not gp.queue:
            await interaction.response.send_message("Очередь пуста.", ephemeral=True)
            return
        lines = []
        if gp.current:
            lines.append(f"**Сейчас:** {gp.current.display_title}  ·  🔊 {gp.volume}%  ·  🔁 {gp.loop_mode}")
        if gp.queue:
            for i, t in enumerate(gp.queue[:20], start=1):
                lines.append(f"`{i:02}` {t.display_title}")
            if gp.q_len() > 20:
                lines.append(f"... + ещё {gp.q_len()-20}")
        msg = "\n".join(lines)
        await interaction.response.send_message(msg)

    @music_group.command(name="remove", description="Удалить трек из очереди по номеру (1..N)")
    @app_commands.describe(index="Номер трека в очереди (1..N)")
    async def cmd_remove(self, interaction: Interaction, index: int):
        gp = self.get_player(interaction.guild_id)
        tr = gp.q_remove(index)
        if tr:
            await interaction.response.send_message(f"🗑 Удалён: **{tr.display_title}**", ephemeral=True)
        else:
            await interaction.response.send_message("Неверный номер.", ephemeral=True)

    @cmd_remove.autocomplete("index")
    async def ac_remove(self, interaction: Interaction, current: str) -> List[app_commands.Choice[int]]:
        gp = self.get_player(interaction.guild_id)
        out: List[app_commands.Choice[int]] = []
        for i, t in enumerate(gp.queue[:25], start=1):
            out.append(app_commands.Choice(name=f"{i}. {t.display_title[:90]}", value=i))
        return out

    @music_group.command(name="move", description="Переместить трек в очереди")
    @app_commands.describe(src="Откуда (1..N)", dst="Куда (1..N)")
    async def cmd_move(self, interaction: Interaction, src: int, dst: int):
        gp = self.get_player(interaction.guild_id)
        ok = gp.q_move(src, dst)
        await interaction.response.send_message("✅ Перемещено." if ok else "❌ Неверные позиции.", ephemeral=True)

    @cmd_move.autocomplete("src")
    async def ac_move_src(self, interaction: Interaction, current: str) -> List[app_commands.Choice[int]]:
        gp = self.get_player(interaction.guild_id)
        return [app_commands.Choice(name=f"{i}. {t.display_title[:90]}", value=i)
                for i, t in enumerate(gp.queue[:25], start=1)]

    @cmd_move.autocomplete("dst")
    async def ac_move_dst(self, interaction: Interaction, current: str) -> List[app_commands.Choice[int]]:
        gp = self.get_player(interaction.guild_id)
        n = min(len(gp.queue), 25)
        return [app_commands.Choice(name=f"→ {i}", value=i) for i in range(1, n + 1)]

    @music_group.command(name="clear", description="Очистить очередь")
    async def cmd_clear(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        gp.q_clear()
        await interaction.response.send_message("🧹 Очередь очищена.", ephemeral=True)

    @music_group.command(name="loop", description="Режим повтора")
    @app_commands.choices(mode=[
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="track", value="track"),
        app_commands.Choice(name="queue", value="queue"),
        app_commands.Choice(name="auto", value="auto"),
    ])
    @app_commands.describe(mode="off / track / queue / auto")
    async def cmd_loop(self, interaction: Interaction, mode: str):
        gp = self.get_player(interaction.guild_id)
        gp.loop_mode = mode
        await interaction.response.send_message(f"🔁 Loop: **{mode}**", ephemeral=True)

    @music_group.command(name="shuffle", description="Перемешать очередь")
    async def cmd_shuffle(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        gp.q_shuffle()
        await interaction.response.send_message("🔀 Перемешал очередь.", ephemeral=True)

    @music_group.command(name="volume", description="Громкость 1–200%")
    @app_commands.describe(level="1..200")
    async def cmd_volume(self, interaction: Interaction, level: int):
        gp = self.get_player(interaction.guild_id)
        gp.set_volume(level)
        await interaction.response.send_message(f"🔊 Громкость: **{gp.volume}%**", ephemeral=True)

    @cmd_volume.autocomplete("level")
    async def ac_volume(self, interaction: Interaction, current: str) -> List[app_commands.Choice[int]]:
        presets = [10, 25, 50, 75, 100, 125, 150, 175, 200]
        return [app_commands.Choice(name=f"{v}%", value=v) for v in presets]

    @music_group.command(name="nowplaying", description="Что сейчас играет")
    async def cmd_nowplaying(self, interaction: Interaction):
        gp = self.get_player(interaction.guild_id)
        cur = gp.current
        if not cur:
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)
            return
        emb = discord.Embed(title="Сейчас играет", description=f"**{cur.display_title}**", color=0x9b59b6)
        if cur.thumbnail:
            emb.set_thumbnail(url=cur.thumbnail)
        emb.add_field(name="Громкость", value=f"{gp.volume}%", inline=True)
        emb.add_field(name="Повтор", value=gp.loop_mode, inline=True)
        await interaction.response.send_message(embed=emb)

    @music_group.command(name="lyrics", description="Показать текст песни (по возможности)")
    @app_commands.describe(title="Название трека (если пусто — берем текущий)")
    async def cmd_lyrics(self, interaction: Interaction, title: Optional[str] = None):
        q = (title or "").strip()
        if not q:
            gp = self.get_player(interaction.guild_id)
            if gp.current:
                q = gp.current.display_title
            else:
                await interaction.response.send_message("Нет текущего трека и не указан title.", ephemeral=True)
                return
        # простой поиск через LRCLIB
        txt = await self._fetch_lyrics(q)
        if not txt:
            await interaction.response.send_message("Текст не найден.", ephemeral=True)
            return
        if len(txt) > 1900:
            txt = txt[:1900] + "…"
        await interaction.response.send_message(f"**{q}**\n\n{txt}")

    async def _fetch_lyrics(self, title: str) -> Optional[str]:
        # без внешних зависимостей: пробуем aiohttp из discord.py
        import aiohttp
        params = {"track_name": title}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
                async with sess.get("https://lrclib.net/api/search", params=params) as r:
                    if r.status != 200:
                        return None
                    arr = await r.json()
                    if not arr:
                        return None
                    for it in arr:
                        if it.get("plainLyrics"):
                            return it["plainLyrics"]
                        if it.get("syncedLyrics"):
                            # вернуть plain из synced, если надо
                            lines = [ln.split("]", 1)[-1].strip() for ln in it["syncedLyrics"].splitlines() if "]" in ln]
                            return "\n".join(lines)
                    return None
        except Exception:
            return None


# ==============================
# setup
# ==============================

async def setup(bot: commands.Bot):
    await bot.add_cog(MusicPower(bot))
