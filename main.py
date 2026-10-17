import ctypes
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog
import urllib.error
import urllib.request
import zipfile
from typing import Any, Callable
import customtkinter as ctk
import mutagen.id3 as id3_tags
from mutagen import MutagenError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image
import yt_dlp
import yt_dlp.version
from yt_dlp.utils import DownloadError

# ================= НАСТРОЙКИ ВЕРСИИ И РЕПОЗИТОРИЯ =================
APP_VERSION = "1.0.5"
# noinspection SpellCheckingInspection
GITHUB_APP_REPO = "Urbanica/YouTube-Downloader-Studio"
APP_API_URL = f"https://api.github.com/repos/{GITHUB_APP_REPO}/releases/latest"
# ==================================================================

# Фикс отображения иконки в панели задач Windows
if sys.platform == "win32":
    try:
        shell32 = getattr(ctypes.windll, "shell32", None)
        if shell32 and hasattr(shell32, "SetCurrentProcessExplicitAppUserModelID"):
            shell32.SetCurrentProcessExplicitAppUserModelID("youtube.downloader.studio.v1")
    except (AttributeError, OSError):
        pass

# ================= ЦВЕТОВАЯ ПАЛИТРА =================
BG_DARK = "#0E0F12"
CARD_BG = "#18191E"
CARD_BORDER = "#282932"
YT_RED = "#E62117"
YT_RED_HOVER = "#B8140D"
BTN_DARK = "#26272E"
BTN_DARK_HOVER = "#363842"
INPUT_BG = "#121316"
INPUT_BORDER = "#2C2E38"
TEXT_WHITE = "#FFFFFF"
TEXT_MUTED = "#8A8D98"

FFMPEG_ZIP_URL = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
FFMPEG_API_URL = "https://api.github.com/repos/yt-dlp/FFmpeg-Builds/releases/latest"

DEFAULT_SETTINGS: dict[str, Any] = {
    "concurrent_fragments": 4,
    "buffer_size_bytes": 2048 * 1024,  # 2 MB
    "audio_codec": "mp3",
    "ffmpeg_build_tag": ""
}
# ====================================================


def get_base_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# noinspection SpellCheckingInspection
def get_resource_path(relative_path: str) -> str:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        return os.path.join(str(meipass), relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


def parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Преобразует строку версии в кортеж чисел (v1.0.2 -> (1, 0, 2))."""
    try:
        return tuple(int(x) for x in re.findall(r'\d+', str(version_str)))
    except (ValueError, TypeError):
        return ()


def normalize_media_url(url: str) -> str:
    """Преобразует ссылки YouTube Music в стандартные ссылки YouTube."""
    clean_url = url.strip()
    return re.sub(r'https?://music\.youtube\.com/', 'https://www.youtube.com/', clean_url)


def find_system_ffmpeg() -> str | None:
    path_env = os.environ.get("PATH", "")
    for folder in path_env.split(os.pathsep):
        clean_folder = folder.strip('"')
        exe_path = os.path.join(clean_folder, "ffmpeg.exe")
        if os.path.isfile(exe_path):
            return clean_folder
    return None


def get_local_ffmpeg_version(ffmpeg_dir: str) -> str:
    """Получает строку версии установленного FFmpeg."""
    try:
        exe_path = os.path.join(ffmpeg_dir, "ffmpeg.exe")
        if not os.path.isfile(exe_path):
            return "Не найден"
        res = subprocess.run(
            [exe_path, "-version"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        first_line = res.stdout.splitlines()[0] if res.stdout else ""
        match = re.search(r'ffmpeg version\s+(\S+)', first_line, re.IGNORECASE)
        return match.group(1) if match else "Установлен"
    except (subprocess.SubprocessError, OSError, IndexError, AttributeError):
        return "Установлен"


def get_og_cover_image(video_id: str) -> str | None:
    music_url = f"https://music.youtube.com/watch?v={video_id}"
    req = urllib.request.Request(
        music_url,
        headers={
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html)
            if match:
                img_url = match.group(1)
                if 'googleusercontent.com' in img_url:
                    return re.sub(r'=w\d+-h\d+.*', '=w1200-h1200-s-l90-rj', img_url)
                return img_url
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    return None


def crop_video_thumbnail(image_path: str) -> None:
    try:
        if not os.path.exists(image_path):
            return
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            width, height = img.size
            if width != height:
                min_side = min(width, height)
                left = (width - min_side) // 2
                top = (height - min_side) // 2
                img = img.crop((left, top, left + min_side, top + min_side))
            img.save(image_path, "JPEG", quality=98)
    except (OSError, ValueError):
        pass


def download_best_cover(video_id: str, thumbnails: list[dict[str, Any]], jpg_path: str) -> bool:
    og_url = get_og_cover_image(video_id)
    if og_url:
        try:
            req = urllib.request.Request(
                og_url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=8) as resp, open(jpg_path, 'wb') as f:
                f.write(resp.read())

            if os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 0:
                with Image.open(jpg_path) as img:
                    img.convert("RGB").save(jpg_path, "JPEG", quality=98)
                return True
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            if os.path.exists(jpg_path):
                try:
                    os.remove(jpg_path)
                except OSError:
                    pass

    for t in reversed(thumbnails):
        url = str(t.get('url', ''))
        if not url.startswith('http'):
            continue
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=8) as resp, open(jpg_path, 'wb') as f:
                f.write(resp.read())

            if os.path.exists(jpg_path) and os.path.getsize(jpg_path) > 0:
                crop_video_thumbnail(jpg_path)
                return True
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            if os.path.exists(jpg_path):
                try:
                    os.remove(jpg_path)
                except OSError:
                    pass
            continue
    return False


def extract_album_name(info: dict[str, Any], fallback_title: str) -> str:
    if info.get('album'):
        return str(info['album']).strip()

    desc = str(info.get('description') or '')
    match_label = re.search(r'·[^\n\r]+\n+([^\n\r]+)\n+℗', desc)
    if match_label:
        album_candidate = match_label.group(1).strip()
        if album_candidate:
            return album_candidate

    match_explicit = re.search(r'(?:Album|Альбом)\s*:\s*([^\n\r·•]+)', desc, re.IGNORECASE)
    if match_explicit:
        return match_explicit.group(1).strip()

    playlist_title = info.get('playlist_title')
    if playlist_title and not any(k in str(playlist_title).lower() for k in ('микс', 'mix', 'playlist', 'плейлист')):
        return str(playlist_title).strip()

    return fallback_title


# noinspection SpellCheckingInspection
def fix_metadata_for_audio(
    audio_path: str,
    cover_path: str,
    title: str,
    artist: str,
    album: str,
    year: str | None = None,
    track_number: str | None = None
) -> None:
    ext = os.path.splitext(audio_path)[1].lower()
    try:
        if ext == ".mp3":
            audio = MP3(audio_path, ID3=id3_tags.ID3)
            try:
                audio.add_tags()
            except MutagenError:
                pass

            if os.path.exists(cover_path):
                with open(cover_path, 'rb') as img:
                    audio.tags.add(
                        id3_tags.APIC(
                            encoding=1,
                            mime='image/jpeg',
                            type=3,
                            desc='Cover',
                            data=img.read()
                        )
                    )

            audio.tags.add(id3_tags.TIT2(encoding=1, text=title))
            audio.tags.add(id3_tags.TPE1(encoding=1, text=artist))
            audio.tags.add(id3_tags.TALB(encoding=1, text=album))

            if year:
                audio.tags.add(id3_tags.TDRC(encoding=1, text=str(year)))
            if track_number:
                audio.tags.add(id3_tags.TRCK(encoding=1, text=str(track_number)))
            audio.save(v2_version=3)

        elif ext == ".m4a":
            audio_m4a = MP4(audio_path)
            audio_m4a['\xa9nam'] = [title]
            audio_m4a['\xa9ART'] = [artist]
            audio_m4a['\xa9alb'] = [album]
            if year:
                audio_m4a['\xa9day'] = [str(year)]
            if track_number and track_number.isdigit():
                audio_m4a['trkn'] = [(int(track_number), 0)]

            if os.path.exists(cover_path):
                with open(cover_path, 'rb') as img:
                    audio_m4a['covr'] = [MP4Cover(img.read(), imageformat=MP4Cover.FORMAT_JPEG)]
            audio_m4a.save()
    except (MutagenError, OSError):
        pass


class YtLogger:
    def __init__(self, app_instance: Any) -> None:
        self.app = app_instance
        self.last_error: str | None = None

    def debug(self, _msg: str) -> None:
        _ = _msg

    def info(self, _msg: str) -> None:
        _ = _msg

    def warning(self, msg: str) -> None:
        if "PO Token" in msg or "SABR" in msg:
            return
        if "HTTP Error 403" in msg:
            self.app._dispatch_ui(self.app.log, f"[!] Предупреждение: {msg}")

    def error(self, msg: str) -> None:
        self.last_error = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', msg).strip()


class UpdateConfirmDialog(ctk.CTkToplevel):
    def __init__(self, parent: 'App', title_text: str, message_text: str, on_confirm: Callable[[], None]) -> None:
        super().__init__(parent)
        self.parent = parent
        self.on_confirm = on_confirm

        self.title(title_text)
        self.geometry("450x240")
        self.resizable(False, False)
        self.configure(fg_color=BG_DARK)
        self.transient(parent)
        self.grab_set()

        frame = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=CARD_BORDER)
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            frame,
            text=f"📦 {title_text}",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=TEXT_WHITE
        ).pack(pady=(12, 6))

        ctk.CTkLabel(
            frame,
            text=message_text,
            font=ctk.CTkFont(size=12),
            text_color=TEXT_MUTED,
            justify="center",
            wraplength=380
        ).pack(pady=(0, 16))

        btn_box = ctk.CTkFrame(frame, fg_color="transparent")
        btn_box.pack(fill="x", padx=16, pady=(0, 8))

        cancel_btn = ctk.CTkButton(
            btn_box,
            text="Позже",
            command=self.destroy,
            width=185,
            height=38,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=BTN_DARK,
            hover_color=BTN_DARK_HOVER,
            border_width=1,
            border_color=CARD_BORDER,
            text_color=TEXT_WHITE
        )
        cancel_btn.pack(side="left")

        ok_btn = ctk.CTkButton(
            btn_box,
            text="⚡ Обновить",
            command=self.confirm_and_close,
            width=185,
            height=38,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=YT_RED,
            hover_color=YT_RED_HOVER,
            text_color=TEXT_WHITE
        )
        ok_btn.pack(side="right")

    def confirm_and_close(self) -> None:
        self.destroy()
        self.on_confirm()


# noinspection SpellCheckingInspection
class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, parent: 'App') -> None:
        super().__init__(parent)
        self.parent = parent

        self.title("Настройки загрузки и ускорения")
        self.geometry("500x680")
        self.resizable(False, False)
        self.configure(fg_color=BG_DARK)
        self.transient(parent)
        self.grab_set()

        frame = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=CARD_BORDER)
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        title_lbl = ctk.CTkLabel(
            frame,
            text="⚡ Настройки сети и форматов",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=TEXT_WHITE
        )
        title_lbl.pack(pady=(14, 10))

        # 1. Потоки
        ctk.CTkLabel(frame, text="Количество потоков (фрагментов):", font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT_WHITE).pack(anchor="w", padx=20, pady=(2, 2))
        self.threads_combo = ctk.CTkComboBox(
            frame,
            values=["1 (Обычный)", "2", "4 (Рекомендуется)", "8 (Высокая скорость)", "16 (Максимум)"],
            width=420,
            height=34,
            fg_color=INPUT_BG,
            border_color=INPUT_BORDER,
            button_color=YT_RED,
            button_hover_color=YT_RED_HOVER,
            dropdown_fg_color=CARD_BG,
            dropdown_text_color=TEXT_WHITE
        )
        self.threads_combo.pack(padx=20, pady=(0, 8))

        # 2. Буфер
        ctk.CTkLabel(frame, text="Размер сетевого буфера:", font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT_WHITE).pack(anchor="w", padx=20, pady=(2, 2))
        self.buffer_combo = ctk.CTkComboBox(
            frame,
            values=["512 КБ", "1 МБ", "2 МБ (Рекомендуется)", "4 МБ", "8 МБ", "16 МБ"],
            width=420,
            height=34,
            fg_color=INPUT_BG,
            border_color=INPUT_BORDER,
            button_color=YT_RED,
            button_hover_color=YT_RED_HOVER,
            dropdown_fg_color=CARD_BG,
            dropdown_text_color=TEXT_WHITE
        )
        self.buffer_combo.pack(padx=20, pady=(0, 8))

        # 3. Аудио кодек
        ctk.CTkLabel(frame, text="Формат сохранения аудио:", font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT_WHITE).pack(anchor="w", padx=20, pady=(2, 2))
        self.audio_fmt_selector = ctk.CTkSegmentedButton(
            frame,
            values=["MP3 (Универсальный)", "M4A (Мгновенный)"],
            width=420,
            height=32,
            fg_color=INPUT_BG,
            selected_color=YT_RED,
            selected_hover_color=YT_RED_HOVER,
            unselected_color=INPUT_BG,
            unselected_hover_color=BTN_DARK,
            text_color=TEXT_WHITE
        )
        self.audio_fmt_selector.pack(padx=20, pady=(0, 12))

        # 4. Шпаргалка
        cheat_frame = ctk.CTkFrame(frame, corner_radius=12, fg_color=INPUT_BG, border_width=1, border_color=INPUT_BORDER)
        cheat_frame.pack(fill="x", padx=20, pady=(0, 16))

        ctk.CTkLabel(
            cheat_frame,
            text="💡 Шпаргалка по выбору настроек:",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=TEXT_WHITE
        ).pack(anchor="w", padx=14, pady=(10, 6))

        scenarios = [
            ("⚡ Домашний интернет 100 Мбит/с (Рекомендуется):", "4 потока  •  Буфер 2 МБ\nМаксимальная скорость, стабильность, защита от блокировок."),
            ("🚀 Сверхбыстрый интернет 300–1000 Мбит/с:", "8 потоков  •  Буфер 4 МБ\nБыстрая загрузка видео высокого разрешения."),
            ("📶 При частых ошибках 403 / нестабильной сети:", "1-2 потока  •  Буфер 1 МБ\nНаивысшая совместимость с серверами YouTube CDN.")
        ]

        for title, desc in scenarios:
            item_frame = ctk.CTkFrame(cheat_frame, fg_color="transparent")
            item_frame.pack(fill="x", padx=14, pady=(2, 6))
            ctk.CTkLabel(
                item_frame,
                text=title,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=YT_RED
            ).pack(anchor="w")
            ctk.CTkLabel(
                item_frame,
                text=desc,
                font=ctk.CTkFont(size=11),
                text_color=TEXT_MUTED,
                justify="left"
            ).pack(anchor="w", padx=(6, 0))

        self.sync_ui_with_settings(
            self.parent.concurrent_fragments,
            self.parent.buffer_size_bytes,
            self.parent.audio_codec
        )

        # 5. Кнопки
        btn_box = ctk.CTkFrame(frame, fg_color="transparent")
        btn_box.pack(fill="x", padx=20, pady=(0, 8))

        reset_btn = ctk.CTkButton(
            btn_box,
            text="🔄 По умолчанию",
            command=self.reset_to_defaults,
            width=200,
            height=40,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=BTN_DARK,
            hover_color=BTN_DARK_HOVER,
            border_width=1,
            border_color=CARD_BORDER,
            text_color=TEXT_WHITE
        )
        reset_btn.pack(side="left")

        save_btn = ctk.CTkButton(
            btn_box,
            text="💾 Сохранить",
            command=self.save_and_close,
            width=210,
            height=40,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=YT_RED,
            hover_color=YT_RED_HOVER,
            text_color=TEXT_WHITE
        )
        save_btn.pack(side="right")

    def sync_ui_with_settings(self, fragments: int, buffer_bytes: int, codec: str) -> None:
        threads_map_rev = {1: "1 (Обычный)", 2: "2", 4: "4 (Рекомендуется)", 8: "8 (Высокая скорость)", 16: "16 (Максимум)"}
        self.threads_combo.set(threads_map_rev.get(fragments, "4 (Рекомендуется)"))

        buffer_map_rev = {
            512 * 1024: "512 КБ",
            1024 * 1024: "1 МБ",
            2048 * 1024: "2 МБ (Рекомендуется)",
            4096 * 1024: "4 МБ",
            8192 * 1024: "8 МБ",
            16384 * 1024: "16 МБ",
        }
        self.buffer_combo.set(buffer_map_rev.get(buffer_bytes, "2 МБ (Рекомендуется)"))
        self.audio_fmt_selector.set("MP3 (Универсальный)" if codec == "mp3" else "M4A (Мгновенный)")

    def reset_to_defaults(self) -> None:
        self.sync_ui_with_settings(
            DEFAULT_SETTINGS["concurrent_fragments"],
            DEFAULT_SETTINGS["buffer_size_bytes"],
            DEFAULT_SETTINGS["audio_codec"]
        )
        self.save_and_close(is_reset=True)

    def save_and_close(self, is_reset: bool = False) -> None:
        threads_str = self.threads_combo.get()
        if "16" in threads_str:
            self.parent.concurrent_fragments = 16
        elif "8" in threads_str:
            self.parent.concurrent_fragments = 8
        elif "4" in threads_str:
            self.parent.concurrent_fragments = 4
        elif "2" in threads_str:
            self.parent.concurrent_fragments = 2
        else:
            self.parent.concurrent_fragments = 1

        buf_str = self.buffer_combo.get()
        if "16" in buf_str:
            self.parent.buffer_size_bytes = 16384 * 1024
        elif "8" in buf_str:
            self.parent.buffer_size_bytes = 8192 * 1024
        elif "4" in buf_str:
            self.parent.buffer_size_bytes = 4096 * 1024
        elif "2" in buf_str:
            self.parent.buffer_size_bytes = 2048 * 1024
        elif "1" in buf_str:
            self.parent.buffer_size_bytes = 1024 * 1024
        else:
            self.parent.buffer_size_bytes = 512 * 1024

        selected_audio = self.audio_fmt_selector.get()
        self.parent.audio_codec = "m4a" if "M4A" in selected_audio else "mp3"
        self.parent.mode_selector.set(f"🎵 {self.parent.audio_codec.upper()} Музыка")

        self.parent.save_settings_to_disk()

        if is_reset:
            self.parent.log("[🔄] Настройки сброшены к стандартным значениям.")
        else:
            self.parent.log(f"[💾] Настройки сохранены: {self.parent.concurrent_fragments} потоков, Буфер {buf_str}, Кодек {self.parent.audio_codec.upper()}")

        self.destroy()


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self._ui_thread_id = threading.get_ident()
        self._ui_events: queue.Queue[tuple[int, Callable[..., Any], tuple[Any, ...]]] = queue.Queue()
        self._auth_process: subprocess.Popen[bytes] | None = None

        self.title(f"YouTube Downloader Studio v{APP_VERSION}")
        self.geometry("740x780")
        self.minsize(700, 720)
        self.configure(fg_color=BG_DARK)

        ico_path = get_resource_path("app.ico")
        if os.path.exists(ico_path):
            try:
                self.iconbitmap(ico_path)
            except (tk.TclError, OSError):
                pass

        ctk.set_appearance_mode("dark")

        base_dir = get_base_dir()
        self.output_folder = os.path.join(base_dir, "downloads")
        self.bin_folder = os.path.join(base_dir, "bin")
        self.legacy_cookie_file_path = os.path.join(base_dir, "cookies.txt")
        app_data_dir = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "YouTubeDownloaderStudio"
        )
        self.cookie_file_path = os.path.join(app_data_dir, "cookies.txt")
        self.webview_profile_path = os.path.join(app_data_dir, "webview")
        self.settings_file_path = os.path.join(base_dir, "settings.json")

        os.makedirs(self.output_folder, exist_ok=True)
        os.makedirs(self.bin_folder, exist_ok=True)
        os.makedirs(app_data_dir, exist_ok=True)

        self.ffmpeg_ready = False
        self.ffmpeg_location: str | None = None

        # Загрузка настроек
        self.concurrent_fragments = DEFAULT_SETTINGS["concurrent_fragments"]
        self.buffer_size_bytes = DEFAULT_SETTINGS["buffer_size_bytes"]
        self.audio_codec = DEFAULT_SETTINGS["audio_codec"]
        self.ffmpeg_build_tag = DEFAULT_SETTINGS["ffmpeg_build_tag"]
        self.load_settings_from_disk()

        self.main_frame = ctk.CTkFrame(
            self,
            corner_radius=20,
            fg_color=CARD_BG,
            border_width=1,
            border_color=CARD_BORDER
        )
        self.main_frame.pack(fill="both", expand=True, padx=24, pady=24)

        # 1. Заголовок
        self.logo_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.logo_frame.pack(pady=(16, 2))

        self.yt_white_label = ctk.CTkLabel(
            self.logo_frame,
            text="You",
            font=ctk.CTkFont(family="Arial", size=26, weight="bold"),
            text_color=TEXT_WHITE
        )
        self.yt_white_label.pack(side="left")

        self.yt_red_label = ctk.CTkLabel(
            self.logo_frame,
            text="Tube",
            font=ctk.CTkFont(family="Arial", size=26, weight="bold"),
            text_color=YT_RED
        )
        self.yt_red_label.pack(side="left")

        # noinspection SpellCheckingInspection
        self.dl_sub_label = ctk.CTkLabel(
            self.main_frame,
            text="D O W N L O A D E R",
            font=ctk.CTkFont(family="Arial", size=11, weight="bold"),
            text_color="#C5C7D0"
        )
        self.dl_sub_label.pack(pady=(0, 4))

        self.studio_badge = ctk.CTkLabel(
            self.main_frame,
            text=f" STUDIO v{APP_VERSION} ",
            font=ctk.CTkFont(family="Arial", size=10, weight="bold"),
            fg_color=YT_RED,
            text_color=TEXT_WHITE,
            corner_radius=6,
            width=88,
            height=20
        )
        self.studio_badge.pack(pady=(0, 12))

        # 2. Переключатель режима
        audio_label = f"🎵 {self.audio_codec.upper()} Музыка"
        self.mode_selector = ctk.CTkSegmentedButton(
            self.main_frame,
            values=[audio_label, "🎬 MP4 Видео"],
            command=self.on_mode_change,
            width=360,
            height=38,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=INPUT_BG,
            selected_color=YT_RED,
            selected_hover_color=YT_RED_HOVER,
            unselected_color=INPUT_BG,
            unselected_hover_color=BTN_DARK,
            text_color=TEXT_WHITE
        )
        self.mode_selector.set(audio_label)
        self.mode_selector.pack(pady=(0, 10))

        # 3. Выбор качества видео
        self.quality_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.quality_label = ctk.CTkLabel(
            self.quality_frame,
            text="Качество видео:",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT_WHITE
        )
        self.quality_label.pack(side="left", padx=(0, 10))

        self.quality_combo = ctk.CTkComboBox(
            self.quality_frame,
            values=["Максимальное (4K / 2K / 1080p)", "1080p Full HD", "720p HD", "480p SD"],
            width=260,
            height=36,
            corner_radius=8,
            fg_color=INPUT_BG,
            border_color=INPUT_BORDER,
            button_color=YT_RED,
            button_hover_color=YT_RED_HOVER,
            dropdown_fg_color=CARD_BG,
            dropdown_hover_color=BTN_DARK,
            dropdown_text_color=TEXT_WHITE,
            text_color=TEXT_WHITE
        )
        self.quality_combo.set("Максимальное (4K / 2K / 1080p)")
        self.quality_combo.pack(side="left")

        # 4. Поле ввода ссылки
        self.input_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.input_frame.pack(fill="x", padx=30, pady=(4, 12))

        self.url_entry = ctk.CTkEntry(
            self.input_frame,
            placeholder_text="Вставьте ссылку на трек, видео или плейлист...",
            placeholder_text_color=TEXT_MUTED,
            height=44,
            corner_radius=10,
            fg_color=INPUT_BG,
            border_color=INPUT_BORDER,
            border_width=1,
            text_color=TEXT_WHITE,
            font=ctk.CTkFont(size=13)
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.paste_btn = ctk.CTkButton(
            self.input_frame,
            text="📋 Вставить",
            command=self.paste_from_clipboard,
            width=110,
            height=44,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=BTN_DARK,
            hover_color=BTN_DARK_HOVER,
            border_width=1,
            border_color=CARD_BORDER,
            text_color=TEXT_WHITE
        )
        self.paste_btn.pack(side="right")

        # 5. Кнопки действий
        self.btn_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.btn_frame.pack(pady=(0, 12))

        self.download_btn = ctk.CTkButton(
            self.btn_frame,
            text="⬇ СКАЧАТЬ",
            command=self.start_download_thread,
            width=150,
            height=42,
            corner_radius=10,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=YT_RED,
            hover_color=YT_RED_HOVER,
            text_color=TEXT_WHITE
        )
        self.download_btn.pack(side="left", padx=4)

        self.folder_btn = ctk.CTkButton(
            self.btn_frame,
            text="📁 Папка",
            command=self.open_downloads_folder,
            fg_color=BTN_DARK,
            hover_color=BTN_DARK_HOVER,
            border_width=1,
            border_color=CARD_BORDER,
            width=110,
            height=42,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT_WHITE
        )
        self.folder_btn.pack(side="left", padx=4)

        self.cookies_btn = ctk.CTkButton(
            self.btn_frame,
            text="🍪 Cookies",
            command=self.open_cookies_menu,
            fg_color=BTN_DARK,
            hover_color=BTN_DARK_HOVER,
            border_width=1,
            border_color=CARD_BORDER,
            width=110,
            height=42,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT_WHITE
        )
        self.cookies_btn.pack(side="left", padx=4)

        self.settings_btn = ctk.CTkButton(
            self.btn_frame,
            text="⚙ Настройки",
            command=self.open_settings_window,
            fg_color=BTN_DARK,
            hover_color=BTN_DARK_HOVER,
            border_width=1,
            border_color=CARD_BORDER,
            width=120,
            height=42,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT_WHITE
        )
        self.settings_btn.pack(side="left", padx=4)

        # 6. Контекстное меню
        self.context_menu = tk.Menu(self, tearoff=0, bg=CARD_BG, fg=TEXT_WHITE, activebackground=YT_RED, activeforeground=TEXT_WHITE)
        self.context_menu.add_command(label="Вставить (Ctrl+V)", command=self.paste_from_clipboard)
        self.context_menu.add_command(label="Копировать (Ctrl+C)", command=self.copy_from_entry)
        self.context_menu.add_command(label="Вырезать (Ctrl+X)", command=self.cut_from_entry)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Выделить всё (Ctrl+A)", command=self.select_all_entry)
        self.context_menu.add_command(label="Очистить", command=lambda: self.url_entry.delete(0, "end"))

        self.url_entry.bind("<Button-3>", self.show_context_menu)
        self.url_entry.bind("<Control-KeyPress>", self.handle_ctrl_hotkeys)

        # 7. Прогресс-бар
        self.progress_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.progress_frame.pack(fill="x", padx=30, pady=(0, 10))

        self.status_label = ctk.CTkLabel(
            self.progress_frame,
            text="Инициализация системы...",
            font=ctk.CTkFont(size=12),
            text_color=TEXT_MUTED
        )
        self.status_label.pack(anchor="w", pady=(0, 4))

        self.progress_bar = ctk.CTkProgressBar(
            self.progress_frame,
            height=8,
            corner_radius=4,
            fg_color=INPUT_BG,
            progress_color=YT_RED
        )
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x")

        # 8. Консоль логов
        self.log_box = ctk.CTkTextbox(
            self.main_frame,
            corner_radius=12,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=INPUT_BG,
            border_width=1,
            border_color=CARD_BORDER,
            text_color="#D8DBE2"
        )
        self.log_box.pack(fill="both", expand=True, padx=30, pady=(0, 16))
        self.log_box.insert("end", f"=== YouTube Downloader Studio v{APP_VERSION} ===\nЗапуск комплексной проверки компонентов...\n")
        self.log_box.configure(state="disabled")

        self.after(25, self._drain_ui_events)
        threading.Thread(target=self.run_startup_diagnostics_and_updates, daemon=True).start()

    def load_settings_from_disk(self) -> None:
        if os.path.exists(self.settings_file_path):
            try:
                with open(self.settings_file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.concurrent_fragments = int(data.get("concurrent_fragments", DEFAULT_SETTINGS["concurrent_fragments"]))
                    self.buffer_size_bytes = int(data.get("buffer_size_bytes", DEFAULT_SETTINGS["buffer_size_bytes"]))
                    self.audio_codec = str(data.get("audio_codec", DEFAULT_SETTINGS["audio_codec"]))
                    self.ffmpeg_build_tag = str(data.get("ffmpeg_build_tag", DEFAULT_SETTINGS["ffmpeg_build_tag"]))
            except (json.JSONDecodeError, OSError, ValueError):
                pass

    def save_settings_to_disk(self) -> None:
        data = {
            "concurrent_fragments": self.concurrent_fragments,
            "buffer_size_bytes": self.buffer_size_bytes,
            "audio_codec": self.audio_codec,
            "ffmpeg_build_tag": self.ffmpeg_build_tag
        }
        try:
            with open(self.settings_file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except OSError as e:
            self.log(f"[✖] Не удалось записать файл настроек: {e}")

    def open_settings_window(self) -> None:
        SettingsWindow(self)

    def show_context_menu(self, event: Any) -> None:
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def paste_from_clipboard(self) -> None:
        try:
            text = self.clipboard_get().strip()
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, text)
        except (tk.TclError, ValueError):
            self.log("[!] Буфер обмена пуст.")

    def copy_from_entry(self) -> None:
        try:
            selected_text = self.url_entry.selection_get()
            self.clipboard_clear()
            self.clipboard_append(selected_text)
        except tk.TclError:
            pass

    def cut_from_entry(self) -> None:
        try:
            selected_text = self.url_entry.selection_get()
            self.clipboard_clear()
            self.clipboard_append(selected_text)
            self.url_entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass

    def select_all_entry(self) -> None:
        self.url_entry.select_range(0, "end")
        self.url_entry.icursor("end")

    def handle_ctrl_hotkeys(self, event: Any) -> str | None:
        if event.keycode == 86:  # Ctrl+V
            self.paste_from_clipboard()
            return "break"
        if event.keycode == 65:  # Ctrl+A
            self.select_all_entry()
            return "break"
        if event.keycode == 67:  # Ctrl+C
            self.copy_from_entry()
            return "break"
        if event.keycode == 88:  # Ctrl+X
            self.cut_from_entry()
            return "break"
        return None

    def open_cookies_menu(self) -> None:
        dialog = ctk.CTkToplevel(self)
        dialog.title("Вход в YouTube")
        dialog.geometry("440x300")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(fg_color=BG_DARK)

        frame = ctk.CTkFrame(dialog, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=CARD_BORDER)
        frame.pack(fill="both", expand=True, padx=14, pady=14)

        ctk.CTkLabel(
            frame,
            text="🔐 Вход в окне приложения",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=TEXT_WHITE
        ).pack(pady=(14, 6))
        ctk.CTkLabel(
            frame,
            text="Нажмите «Войти», авторизуйтесь на YouTube и дождитесь\nвозврата на главную страницу, затем закройте окно входа.\nGoogle может ограничивать авторизацию во встроенных окнах.",
            font=ctk.CTkFont(size=11),
            text_color=TEXT_MUTED,
            justify="center",
            wraplength=380
        ).pack(pady=(0, 12))

        button_frame = ctk.CTkFrame(frame, fg_color="transparent")
        button_frame.pack(fill="x", padx=16, pady=(0, 12))

        def start_embedded_login() -> None:
            dialog.destroy()
            self.start_embedded_youtube_login()

        def choose_cookie_file() -> None:
            dialog.destroy()
            self._pick_cookies_file()

        ctk.CTkButton(
            button_frame,
            text="🌐 Войти",
            command=start_embedded_login,
            width=180,
            height=40,
            fg_color=YT_RED,
            hover_color=YT_RED_HOVER
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            button_frame,
            text="📁 Импорт cookies.txt",
            command=choose_cookie_file,
            width=180,
            height=40,
            fg_color=BTN_DARK,
            hover_color=BTN_DARK_HOVER
        ).pack(side="right", padx=4)

        if os.path.isfile(self.cookie_file_path) or os.path.isfile(self.legacy_cookie_file_path):
            def delete_saved_cookies() -> None:
                if self._auth_process and self._auth_process.poll() is None:
                    self.log("[!] Закройте окно входа, затем повторите удаление.")
                    return
                try:
                    for path in (self.cookie_file_path, self.legacy_cookie_file_path):
                        if os.path.isfile(path):
                            os.remove(path)
                    if os.path.isdir(self.webview_profile_path):
                        shutil.rmtree(self.webview_profile_path)
                    self.log("[🗑] Сохраненные cookies удалены.")
                except OSError as error:
                    self.log(f"[✖] Ошибка удаления cookies: {error}")
                dialog.destroy()

            ctk.CTkButton(
                frame,
                text="🗑 Удалить сохраненный вход",
                command=delete_saved_cookies,
                width=370,
                height=32,
                fg_color="#7A130E",
                hover_color="#550D0A"
            ).pack(padx=20, pady=(0, 8))

    def _pick_cookies_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Выберите файл cookies.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as src, \
                     open(self.cookie_file_path, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
                self.log("[✔] Файл cookies импортирован в пользовательское хранилище.\n")
            except OSError as e:
                self.log(f"[✖] Ошибка cookies: {e}\n")

    def start_embedded_youtube_login(self) -> None:
        if self._auth_process and self._auth_process.poll() is None:
            self.log("[!] Окно входа в YouTube уже открыто.")
            return

        result_fd, result_path = tempfile.mkstemp(prefix="youtube-auth-", suffix=".json")
        os.close(result_fd)
        os.remove(result_path)

        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(os.path.abspath(__file__))
        command.extend([
            "--youtube-auth-helper",
            self.cookie_file_path,
            self.webview_profile_path,
            result_path,
        ])

        try:
            self._auth_process = subprocess.Popen(command, cwd=get_base_dir())
        except OSError as error:
            try:
                os.remove(result_path)
            except OSError:
                pass
            self.log(f"[✖] Не удалось открыть встроенное окно входа: {error}")
            return

        self.log("[🌐] Открыто окно входа в YouTube. После авторизации вернитесь на YouTube и закройте его.")
        threading.Thread(
            target=self._wait_for_embedded_login,
            args=(self._auth_process, result_path),
            daemon=True
        ).start()

    def _wait_for_embedded_login(self, process: subprocess.Popen[bytes], result_path: str) -> None:
        exit_code = process.wait()
        result: dict[str, Any] = {}
        try:
            with open(result_path, "r", encoding="utf-8") as result_file:
                loaded_result = json.load(result_file)
                if isinstance(loaded_result, dict):
                    result = loaded_result
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            try:
                os.remove(result_path)
            except OSError:
                pass
        self._dispatch_ui(self._finish_embedded_login, exit_code, result)

    def _finish_embedded_login(self, exit_code: int, result: dict[str, Any]) -> None:
        self._auth_process = None
        if exit_code == 0 and result.get("success") and os.path.isfile(self.cookie_file_path):
            self.log("[✔] Вход сохранен. Следующая загрузка будет использовать эту сессию.")
            return

        error = str(result.get("error") or "Окно входа закрыто до завершения авторизации.")
        self.log(f"[✖] Не удалось сохранить вход: {error}")

    def update_progress(self, fraction: float, status_text: str) -> None:
        if threading.get_ident() != self._ui_thread_id:
            self._dispatch_ui(self.update_progress, fraction, status_text)
            return
        self.progress_bar.set(max(0.0, min(1.0, fraction)))
        self.status_label.configure(text=status_text)

    def _dispatch_ui(
        self,
        callback: Callable[..., Any],
        *args: Any,
        delay_ms: int = 0
    ) -> None:
        if threading.get_ident() == self._ui_thread_id:
            if delay_ms:
                self.after(delay_ms, callback, *args)
            else:
                callback(*args)
            return
        self._ui_events.put((delay_ms, callback, args))

    def _drain_ui_events(self) -> None:
        for _ in range(100):
            try:
                delay_ms, callback, args = self._ui_events.get_nowait()
            except queue.Empty:
                break

            if delay_ms:
                self.after(delay_ms, callback, *args)
            else:
                callback(*args)

        self.after(25, self._drain_ui_events)

    def progress_hook(self, d: dict[str, Any]) -> None:
        status = d.get('status')
        if status == 'downloading':
            total = float(d.get('total_bytes') or d.get('total_bytes_estimate') or 0)
            downloaded = float(d.get('downloaded_bytes') or 0)
            speed = float(d.get('speed') or 0)

            if total > 0:
                percent_str = f"{(downloaded / total) * 100:.1f}%"
                size_str = f"{downloaded / 1048576:.1f} MB / {total / 1048576:.1f} MB"
                frac = downloaded / total
            else:
                percent_str = "--%"
                size_str = f"{downloaded / 1048576:.1f} MB"
                frac = 0.0

            speed_str = f"{speed / 1048576:.1f} MB/s" if speed > 0 else "-- MB/s"

            eta_raw = d.get('eta')
            eta_str = "--:--"
            if eta_raw is not None:
                try:
                    eta_seconds = int(float(str(eta_raw)))
                    mins, secs = divmod(eta_seconds, 60)
                    eta_str = f"{mins:02d}:{secs:02d}"
                except (ValueError, TypeError):
                    eta_str = "--:--"

            status_text = f"Загрузка: {percent_str} ({size_str}) | {speed_str} | ETA {eta_str}"
            self._dispatch_ui(self.update_progress, frac, status_text)

        elif status == 'finished':
            self._dispatch_ui(self.update_progress, 1.0, "Склейка / финализация через FFmpeg...")

    def log(self, message: str) -> None:
        if threading.get_ident() != self._ui_thread_id:
            self._dispatch_ui(self.log, message)
            return
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def on_mode_change(self, selected_mode: str) -> None:
        if "MP4" in selected_mode:
            self.quality_frame.pack(after=self.mode_selector, pady=(0, 10))
        else:
            self.quality_frame.pack_forget()

    def open_downloads_folder(self) -> None:
        if os.path.exists(self.output_folder):
            os.startfile(self.output_folder)

    # noinspection SpellCheckingInspection
    def run_startup_diagnostics_and_updates(self) -> None:
        """Комплексная проверка всех компонентов и обновлений самого приложения."""
        self.check_app_self_update()
        self.ensure_ffmpeg_installed()
        self.auto_check_and_update_ytdlp()

        if os.path.exists(self.cookie_file_path):
            self.log("[🍪] Сохраненный вход YouTube активен.")
        elif os.path.exists(self.legacy_cookie_file_path):
            self.log("[🍪] Найден файл cookies.txt рядом с приложением.")

        self._dispatch_ui(self.update_progress, 0.0, "Готов к загрузке")
        self.log("[🚀] Система полностью готова к работе!\n" + "="*35 + "\n")

    # ================= СИСТЕМА САМООБНОВЛЕНИЯ ПРИЛОЖЕНИЯ =================
    def check_app_self_update(self) -> None:
        try:
            req = urllib.request.Request(
                APP_API_URL,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"}
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                latest_tag = str(data.get("tag_name", "")).strip().lstrip("v")

                exe_download_url = ""
                for asset in data.get("assets", []):
                    name = str(asset.get("name", ""))
                    if name.endswith(".exe"):
                        url_val = asset.get("browser_download_url")
                        if isinstance(url_val, str):
                            exe_download_url = url_val
                            break

            cur_v = parse_version_tuple(APP_VERSION)
            lat_v = parse_version_tuple(latest_tag)

            if lat_v > cur_v and exe_download_url:
                self.log(f"[🚀] Доступна новая версия программы: v{latest_tag} (текущая: v{APP_VERSION})!")
                self._dispatch_ui(
                    self._show_app_update_dialog,
                    latest_tag,
                    exe_download_url,
                    delay_ms=500
                )
            else:
                self.log(f"[✔] Версия приложения актуальна (v{APP_VERSION}).")

        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass

    def _show_app_update_dialog(self, latest_tag: str, download_url: str) -> None:
        msg = f"Вышла новая версия приложения: v{latest_tag}!\nОбновить и автоматически перезапустить программу?"
        UpdateConfirmDialog(
            self,
            "Обновление программы",
            msg,
            lambda: threading.Thread(
                target=lambda: self.download_and_apply_app_update(download_url),
                daemon=True
            ).start()
        )

    # noinspection SpellCheckingInspection
    def download_and_apply_app_update(self, download_url: str) -> None:
        if not getattr(sys, 'frozen', False):
            self.log("[ℹ] Вы запустили программу из исходного кода (PyCharm).")
            self.log("[ℹ] В режиме разработки автозамена .exe не производится.")
            self.log("[✔] Запустите скомпилированный .exe файл для проверки обновления!\n")
            return

        self.log("[⏳] Загрузка новой версии приложения с GitHub...")
        self._dispatch_ui(self.update_progress, 0.0, "Загрузка обновления...")
        base_dir = get_base_dir()
        current_exe = sys.executable
        temp_exe = os.path.join(base_dir, "update_new.tmp")
        bat_path = os.path.join(base_dir, "updater.bat")

        try:
            req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = resp.read()
                if len(data) < 1024 * 500:
                    self.log("[✖] Ошибка: скачанный файл поврежден.")
                    return
                with open(temp_exe, "wb") as f:
                    f.write(data)

            self._dispatch_ui(self.update_progress, 1.0, "Замена и перезапуск...")
            self.log("[+] Запуск обновления...")

            bat_script = f"""@echo off
chcp 65001 > nul
set PYINSTALLER_RESET_ENVIRONMENT=1
set _PYI_PARENT_PROCESS_LEVEL=
set _MEIPASS=
set _PYI_ARCHIVE_FILE=
set _PYI_APPLICATION_HOME_DIR=
set _PYI_SPLASH_IPC=

timeout /t 2 /nobreak > nul

:retry
move /y "{temp_exe}" "{current_exe}" > nul 2>&1
if errorlevel 1 (
    timeout /t 1 /nobreak > nul
    goto retry
)

start "" "{current_exe}"
del "%~f0"
"""
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(bat_script)

            clean_env = os.environ.copy()
            clean_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            for k in list(clean_env.keys()):
                if k.startswith("_PYI_") or k.startswith("_MEI"):
                    del clean_env[k]

            subprocess.Popen(["cmd.exe", "/c", bat_path], env=clean_env, creationflags=subprocess.CREATE_NO_WINDOW)
            self._dispatch_ui(self.destroy)

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.log(f"[✖] Не удалось обновить приложение: {e}\n")
            if os.path.exists(temp_exe):
                try:
                    os.remove(temp_exe)
                except OSError:
                    pass

    # ================= YT-DLP И FFMPEG =================
    # noinspection SpellCheckingInspection
    def auto_check_and_update_ytdlp(self) -> None:
        current_version = getattr(yt_dlp.version, "__version__", "0.0.0")
        self.log(f"[🔍] Проверка версии yt-dlp (установлена: {current_version})...")

        try:
            req = urllib.request.Request(
                "https://pypi.org/pypi/yt-dlp/json",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pypi_data = json.loads(resp.read().decode("utf-8"))
                latest_version = pypi_data.get("info", {}).get("version", current_version)

            cur_v = parse_version_tuple(current_version)
            lat_v = parse_version_tuple(latest_version)

            if lat_v > cur_v:
                self.log(f"[⏳] Найдено обновление yt-dlp ({current_version} -> {latest_version})...")
                self._dispatch_ui(self.update_progress, 0.5, f"Обновление yt-dlp до {latest_version}...")

                if not getattr(sys, 'frozen', False):
                    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp", "--no-warn-script-location"]
                    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
                    if result.returncode == 0:
                        self.log(f"[✔] Ядро yt-dlp успешно обновлено до версии {latest_version}!\n")
                    else:
                        self.log(f"[!] Не удалось обновить yt-dlp через pip: {result.stderr}\n")
                else:
                    self.log(f"[ℹ] Доступна новая версия yt-dlp {latest_version}.\n")
            else:
                self.log(f"[✔] Ядро yt-dlp актуально (v{current_version}).\n")

        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as e:
            self.log(f"[!] Проверка обновлений yt-dlp пропущена: {e}\n")

    # noinspection SpellCheckingInspection
    def ensure_ffmpeg_installed(self) -> None:
        base_dir = get_base_dir()
        local_ffmpeg = os.path.join(self.bin_folder, "ffmpeg.exe")
        direct_ffmpeg = os.path.join(base_dir, "ffmpeg.exe")

        if os.path.isfile(local_ffmpeg):
            active_dir = self.bin_folder
        elif os.path.isfile(direct_ffmpeg):
            active_dir = base_dir
        else:
            active_dir = find_system_ffmpeg()

        if active_dir:
            self.ffmpeg_location = active_dir
            self.ffmpeg_ready = True
            ver = get_local_ffmpeg_version(active_dir)
            self.log(f"[✔] FFmpeg обнаружен (версия: {ver}).")
            self.check_ffmpeg_github_update()
            return

        self.download_and_extract_ffmpeg(is_update=False)

    def check_ffmpeg_github_update(self) -> None:
        try:
            req = urllib.request.Request(
                FFMPEG_API_URL,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"}
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                latest_tag = str(data.get("tag_name", "")).strip()

            if latest_tag and self.ffmpeg_build_tag and latest_tag != self.ffmpeg_build_tag:
                self._dispatch_ui(self._show_ffmpeg_update_dialog, latest_tag, delay_ms=500)
            elif latest_tag and not self.ffmpeg_build_tag:
                self.ffmpeg_build_tag = latest_tag
                self.save_settings_to_disk()

        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass

    def _show_ffmpeg_update_dialog(self, latest_tag: str) -> None:
        msg = f"Найдена свежая сборка FFmpeg ({latest_tag}).\nХотите загрузить и обновить компонент (~70 МБ)?"
        UpdateConfirmDialog(
            self,
            "Обновление FFmpeg",
            msg,
            lambda: threading.Thread(
                target=lambda: self.download_and_extract_ffmpeg(True, latest_tag),
                daemon=True
            ).start()
        )

    # noinspection SpellCheckingInspection
    def download_and_extract_ffmpeg(self, is_update: bool = False, tag_name: str = "") -> None:
        action_text = "Обновление" if is_update else "Загрузка"
        self.log(f"[⏳] {action_text} FFmpeg с GitHub (~70 МБ)...")
        self._dispatch_ui(self.update_progress, 0.0, f"{action_text} FFmpeg...")

        try:
            req = urllib.request.Request(
                FFMPEG_ZIP_URL,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=35) as resp:
                total_header = resp.headers.get('content-length')
                data = bytearray()
                if total_header and total_header.isdigit():
                    total_bytes = int(total_header)
                    downloaded = 0
                    while True:
                        chunk = resp.read(1024 * 512)
                        if not chunk:
                            break
                        data.extend(chunk)
                        downloaded += len(chunk)
                        frac = downloaded / total_bytes
                        self._dispatch_ui(
                            self.update_progress,
                            frac,
                            f"{action_text} FFmpeg: {int(frac * 100)}%"
                        )
                else:
                    data = bytearray(resp.read())

            self._dispatch_ui(self.update_progress, 1.0, "Распаковка FFmpeg...")
            self.log("[+] Распаковка FFmpeg в папку bin...")

            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for file_info in z.infolist():
                    base_name = os.path.basename(file_info.filename)
                    if base_name in ("ffmpeg.exe", "ffprobe.exe"):
                        target_file = os.path.join(self.bin_folder, base_name)
                        with z.open(file_info) as source, open(target_file, "wb") as target:
                            target.write(source.read())

            self.ffmpeg_location = self.bin_folder
            self.ffmpeg_ready = True
            if tag_name:
                self.ffmpeg_build_tag = tag_name
                self.save_settings_to_disk()

            self._dispatch_ui(self.update_progress, 0.0, "Готов к загрузке")
            self.log(f"[✔] FFmpeg успешно {'обновлен' if is_update else 'установлен'} и готов к работе!\n")
        except (urllib.error.URLError, TimeoutError, OSError, zipfile.BadZipFile) as e:
            self.log(f"[✖] Ошибка при загрузке FFmpeg: {e}\n")
            self._dispatch_ui(self.update_progress, 0.0, "Ошибка FFmpeg")

    # ================= СКАЧИВАНИЕ =================
    def start_download_thread(self) -> None:
        url = self.url_entry.get().strip()
        if not url:
            self.log("[✖] Ошибка: ссылка не может быть пустой.")
            return

        if not self.ffmpeg_ready and not os.path.isfile(os.path.join(self.bin_folder, "ffmpeg.exe")):
            self.log("[⏳] Подождите завершения инициализации FFmpeg...")
            return

        self.download_btn.configure(state="disabled", text="Скачивание...", fg_color="#7A130E")
        self.update_progress(0.0, "Подключение к YouTube...")
        mode = self.mode_selector.get()
        quality = self.quality_combo.get()

        threading.Thread(
            target=lambda: self.run_download(url, mode, quality),
            daemon=True
        ).start()

    def run_download(self, url: str, mode: str, quality_str: str) -> None:
        try:
            if "MP4" not in mode:
                self.process_audio(url)
            else:
                self.process_video(url, quality_str)
        finally:
            self._dispatch_ui(self._restore_download_button)

    def _restore_download_button(self) -> None:
        self.download_btn.configure(state="normal", text="⬇ СКАЧАТЬ", fg_color=YT_RED)

    def _build_base_ydl_opts(self) -> dict[str, Any]:
        """Формирует стабильный базовый набор параметров yt-dlp."""
        opts: dict[str, Any] = {
            'concurrent_fragment_downloads': self.concurrent_fragments,
            'buffersize': self.buffer_size_bytes,
            'progress_hooks': [self.progress_hook],
            'logger': YtLogger(self),
        }
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location
        if os.path.exists(self.cookie_file_path):
            opts['cookiefile'] = self.cookie_file_path
        elif os.path.exists(self.legacy_cookie_file_path):
            opts['cookiefile'] = self.legacy_cookie_file_path
        return opts

    # noinspection SpellCheckingInspection
    def process_audio(self, url: str) -> None:
        codec = self.audio_codec
        target_url = normalize_media_url(url)
        self.log(f"[+] Загрузка {codec.upper()} ({self.concurrent_fragments} потоков): {target_url}")

        ydl_opts = self._build_base_ydl_opts()
        ydl_opts.update({
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(self.output_folder, '%(id)s.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': codec,
                'preferredquality': '0',
            }],
        })

        if "watch?v=" in target_url:
            ydl_opts['noplaylist'] = True

        try:
            self._execute_download_with_fallback(ydl_opts, target_url, is_audio=True, codec=codec)
        except Exception as e:
            self.log(f"[✖] Ошибка: {e}\n")
            self.update_progress(0.0, "Ошибка")

    # noinspection SpellCheckingInspection
    def process_video(self, url: str, quality_str: str) -> None:
        target_url = normalize_media_url(url)
        self.log(f"[+] Загрузка видео ({quality_str} | {self.concurrent_fragments} потоков): {target_url}")

        quality_map = {
            "Максимальное (4K / 2K / 1080p)": "bestvideo+bestaudio/best",
            "1080p Full HD": "bestvideo[height<=?1080]+bestaudio/best[height<=?1080]/best",
            "720p HD": "bestvideo[height<=?720]+bestaudio/best[height<=?720]/best",
            "480p SD": "bestvideo[height<=?480]+bestaudio/best[height<=?480]/best",
        }
        fmt = quality_map.get(quality_str, "bestvideo+bestaudio/best")

        ydl_opts = self._build_base_ydl_opts()
        ydl_opts.update({
            'format': fmt,
            'outtmpl': os.path.join(self.output_folder, '%(title)s.%(ext)s'),
            'merge_output_format': 'mp4',
        })

        if "watch?v=" in target_url:
            ydl_opts['noplaylist'] = True

        try:
            self._execute_download_with_fallback(ydl_opts, target_url, is_audio=False)
        except Exception as e:
            self.log(f"[✖] Ошибка: {e}\n")
            self.update_progress(0.0, "Ошибка")

    def _execute_download_with_fallback(self, opts: dict[str, Any], target_url: str, is_audio: bool, codec: str = "mp3") -> None:
        """Выполняет загрузку с интеллектуальным откатом при 403 Forbidden."""
        logger = opts.get('logger')
        if isinstance(logger, YtLogger):
            logger.last_error = None
        try:
            self._run_ytdlp_process(opts, target_url, is_audio, codec)
        except DownloadError as e:
            err_text = str(e)
            if "the page needs to be reloaded" in err_text.lower():
                self.log("[✖] YouTube вернул страницу с сообщением о перезагрузке вместо данных видео.")
                self.log("[ℹ] Повторите попытку позже. Если ошибка повторяется, проверьте видео в браузере и обновите приложение.")
                self.update_progress(0.0, "Ошибка YouTube — попробуйте позже")
                return

            # Снижаем нагрузку, сохраняя выбранный источник авторизации.
            has_cookie_source = 'cookiefile' in opts
            if ("403" in err_text or "Forbidden" in err_text) and has_cookie_source:
                self.log("[!] Ошибка 403. Повторяем загрузку с авторизацией и одним потоком...")
                fallback_opts = dict(opts)
                fallback_opts['concurrent_fragment_downloads'] = 1
                if isinstance(logger, YtLogger):
                    logger.last_error = None
                try:
                    self._run_ytdlp_process(fallback_opts, target_url, is_audio, codec)
                    return
                except DownloadError as fe:
                    error_message = logger.last_error if isinstance(logger, YtLogger) else None
                    fallback_error = error_message or str(fe)
                    if "sign in to confirm" in fallback_error.lower():
                        self._log_authentication_help()
                    else:
                        self.log(f"[✖] Ошибка повторной попытки: {fallback_error}\n")
                    self.update_progress(0.0, "Ошибка 403")
                    return

            if "Sign in to confirm" in err_text:
                self.log("[✖] YouTube требует вход в аккаунт (Бот-проверка).")
                self._log_authentication_help()
            else:
                error_message = logger.last_error if isinstance(logger, YtLogger) else None
                self.log(f"[✖] Ошибка yt-dlp: {error_message or e}\n")
            self.update_progress(0.0, "Ошибка скачивания")

    def _log_authentication_help(self) -> None:
        self.log("[ℹ] Попробуйте войти заново через кнопку Cookies. Google может запрещать вход во встроенном окне.\n")

    def _run_ytdlp_process(self, opts: dict[str, Any], target_url: str, is_audio: bool, codec: str = "mp3") -> None:
        # noinspection PyTypeChecker
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(target_url, download=True)
            if not isinstance(info, dict):
                return

            if is_audio:
                entries = info.get('entries', [info]) if 'entries' in info and info['entries'] else [info]
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    video_id = str(item.get('id') or 'track')
                    title = str(item.get('track') or item.get('title') or 'Unknown Title').strip()
                    artist = str(item.get('artist') or item.get('uploader') or item.get('channel') or 'YouTube').strip()
                    album = extract_album_name(item, fallback_title=title)
                    year_val = item.get('release_year') or (str(item.get('upload_date') or '')[:4] if item.get('upload_date') else None)

                    audio_path = os.path.join(self.output_folder, f"{video_id}.{codec}")
                    jpg_path = os.path.join(self.output_folder, f"{video_id}.jpg")

                    download_best_cover(video_id, item.get('thumbnails', []), jpg_path)

                    if os.path.exists(audio_path):
                        fix_metadata_for_audio(
                            audio_path=audio_path,
                            cover_path=jpg_path,
                            title=title,
                            artist=artist,
                            album=album,
                            year=str(year_val) if year_val else None,
                        )
                        if os.path.exists(jpg_path):
                            os.remove(jpg_path)

                        safe_name = "".join(c for c in f"{artist} - {title}" if c not in r'\/:*?"<>|').strip()
                        final_path = os.path.join(self.output_folder, f"{safe_name}.{codec}")

                        if os.path.exists(final_path):
                            os.remove(final_path)
                        os.rename(audio_path, final_path)
                        self.log(f"[✔] Аудио сохранено: {safe_name}.{codec}\n")

                self.update_progress(1.0, "Загрузка завершена!")
                self.url_entry.delete(0, "end")
            else:
                title = info.get('title', 'Видео')
                width = info.get('width', '?')
                height = info.get('height', '?')
                self.log(f"[✔] Успешно скачано видео ({width}x{height}p):\n    -> {title}.mp4\n")
                self.update_progress(1.0, f"Готово ({height}p)")
                self.url_entry.delete(0, "end")


def run_youtube_auth_helper(cookie_path: str, storage_path: str, result_path: str) -> int:
    result: dict[str, Any]
    try:
        from youtube_auth import run_youtube_login

        success, error = run_youtube_login(cookie_path, storage_path)
        result = {
            "success": success,
            "error": error,
        }
    except (ImportError, OSError, RuntimeError, ValueError, AttributeError, TypeError) as error:
        result = {"success": False, "error": str(error)}

    try:
        with open(result_path, "w", encoding="utf-8") as result_file:
            json.dump(result, result_file, ensure_ascii=False)
    except OSError:
        return 1
    return 0 if result["success"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--youtube-auth-helper":
        sys.exit(run_youtube_auth_helper(sys.argv[2], sys.argv[3], sys.argv[4]))
    app = App()
    app.mainloop()