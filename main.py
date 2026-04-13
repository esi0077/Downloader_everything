import logging
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

import requests
import yt_dlp
from yt_dlp.utils import DownloadError

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "Downloader Everything"
VIDEO_DOWNLOAD_FOLDER = "downloads/videos"
AUDIO_DOWNLOAD_FOLDER = "downloads/audios"
REQUEST_TIMEOUT = 240

FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_TOOLS_DIR = os.path.join(".tools", "ffmpeg")
FFMPEG_BIN_DIR = os.path.join(FFMPEG_TOOLS_DIR, "bin")
FFMPEG_EXE_PATH = os.path.join(FFMPEG_BIN_DIR, "ffmpeg.exe")

os.makedirs(VIDEO_DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(AUDIO_DOWNLOAD_FOLDER, exist_ok=True)

logging.basicConfig(
    filename="downloader.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

FFMPEG_LOCK = threading.Lock()
FFMPEG_STATE = {"message": "FFmpeg: waiting", "progress": 0.0}


def set_ffmpeg_state(message, progress=None):
    with FFMPEG_LOCK:
        FFMPEG_STATE["message"] = message
        if progress is not None:
            FFMPEG_STATE["progress"] = max(0.0, min(1.0, float(progress)))


def get_ffmpeg_state():
    with FFMPEG_LOCK:
        return FFMPEG_STATE["message"], FFMPEG_STATE["progress"]


def sanitize_filename(value):
    cleaned = "".join(c if c.isalnum() or c in " -_." else "_" for c in str(value)).strip(" .")
    return cleaned or "download"


def ensure_unique_path(folder, filename):
    base = Path(filename).stem
    ext = Path(filename).suffix
    candidate = os.path.join(folder, filename)
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base} ({index}){ext}")
        index += 1
    return candidate, os.path.basename(candidate)


def get_output_folder(file_type):
    return AUDIO_DOWNLOAD_FOLDER if file_type == "audio" else VIDEO_DOWNLOAD_FOLDER


def ensure_ffmpeg_available():
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        set_ffmpeg_state("FFmpeg: using system installation", 1.0)
        return system_ffmpeg

    local_ffmpeg = Path(FFMPEG_EXE_PATH)
    if local_ffmpeg.exists():
        set_ffmpeg_state("FFmpeg: ready (local installation)", 1.0)
        return str(local_ffmpeg.resolve())

    os.makedirs(FFMPEG_TOOLS_DIR, exist_ok=True)
    zip_target = os.path.join(FFMPEG_TOOLS_DIR, "ffmpeg.zip")

    set_ffmpeg_state("FFmpeg: downloading installer...", 0.02)
    with requests.get(FFMPEG_DOWNLOAD_URL, stream=True, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        total_size = int(response.headers.get("content-length", "0"))
        downloaded = 0
        with open(zip_target, "wb") as zip_file:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    zip_file.write(chunk)
                    if total_size > 0:
                        downloaded += len(chunk)
                        set_ffmpeg_state("FFmpeg: downloading installer...", (downloaded / total_size) * 0.75)

    set_ffmpeg_state("FFmpeg: extracting files...", 0.78)
    with zipfile.ZipFile(zip_target, "r") as zip_ref:
        members = zip_ref.infolist()
        total_members = max(1, len(members))
        for idx, member in enumerate(members, start=1):
            zip_ref.extract(member, FFMPEG_TOOLS_DIR)
            set_ffmpeg_state("FFmpeg: extracting files...", 0.75 + (idx / total_members) * 0.20)

    extracted_bin = None
    for root, _, files in os.walk(FFMPEG_TOOLS_DIR):
        if "ffmpeg.exe" in files and "bin" in root.lower():
            extracted_bin = root
            break

    if not extracted_bin:
        raise RuntimeError("FFmpeg install failed: ffmpeg.exe not found.")

    os.makedirs(FFMPEG_BIN_DIR, exist_ok=True)
    set_ffmpeg_state("FFmpeg: finalizing installation...", 0.96)
    for binary_name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
        src = os.path.join(extracted_bin, binary_name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(FFMPEG_BIN_DIR, binary_name))

    if os.path.exists(zip_target):
        os.remove(zip_target)

    if not os.path.exists(FFMPEG_EXE_PATH):
        raise RuntimeError("FFmpeg install failed: copy step failed.")

    set_ffmpeg_state("FFmpeg: installed successfully", 1.0)
    return str(Path(FFMPEG_EXE_PATH).resolve())


def resolve_entry_url(entry):
    candidate = entry.get("webpage_url") or entry.get("url")
    if not candidate:
        return None
    if candidate.startswith("http://") or candidate.startswith("https://"):
        return normalize_download_url(candidate)
    if entry.get("ie_key") == "Youtube" and entry.get("id"):
        return f"https://www.youtube.com/watch?v={entry['id']}"
    return candidate


def normalize_download_url(url):
    """Use standard watch URLs for YouTube Shorts so merge/download stays reliable."""
    url = (url or "").strip()
    match = re.search(r"(?:youtube\.com/)shorts/([0-9A-Za-z_-]{11})", url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return url


class DownloadWorker(QThread):
    progress = Signal(int)
    current_item = Signal(str)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, urls, file_type, quality, output_folder):
        super().__init__()
        self.urls = urls
        self.file_type = file_type
        self.quality = quality
        self.output_folder = output_folder
        self.total_items = 0
        self.finished_items = 0

    def _emit_overall_progress(self, current_item_percent):
        if self.total_items <= 0:
            self.progress.emit(0)
            return
        overall = ((self.finished_items + current_item_percent / 100.0) / self.total_items) * 100.0
        self.progress.emit(max(0, min(100, int(overall))))

    def _build_ydl_opts(self, output_path):
        opts = {"outtmpl": output_path, "quiet": True, "noplaylist": True}

        def hook(d):
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total > 0:
                    current_pct = (downloaded / total) * 100.0
                    self._emit_overall_progress(current_pct)
            elif status == "finished":
                self._emit_overall_progress(100.0)

        opts["progress_hooks"] = [hook]

        if self.file_type == "audio":
            ffmpeg_path = ensure_ffmpeg_available()
            opts.update(
                {
                    "format": "bestaudio/best",
                    "postprocessors": [
                        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": self.quality},
                        {"key": "FFmpegMetadata"},
                    ],
                    "final_ext": "mp3",
                    "prefer_ffmpeg": True,
                    "ffmpeg_location": str(Path(ffmpeg_path).parent),
                }
            )
        else:
            # Merging separate video+audio into MP4 requires FFmpeg.
            ffmpeg_path = ensure_ffmpeg_available()
            opts.update(
                {
                    "format": "bestvideo+bestaudio/best",
                    "merge_output_format": "mp4",
                    "prefer_ffmpeg": True,
                    "ffmpeg_location": str(Path(ffmpeg_path).parent),
                    "postprocessors": [{"key": "FFmpegMetadata"}],
                }
            )
        return opts

    def _get_final_target(self, info):
        title = sanitize_filename(info.get("title", f"item_{int(time.time())}"))
        ext = "mp3" if self.file_type == "audio" else "mp4"
        os.makedirs(self.output_folder, exist_ok=True)
        return ensure_unique_path(self.output_folder, f"{title}.{ext}")

    def _collect_targets(self):
        targets = []
        probe_opts = {"quiet": True, "extract_flat": "in_playlist", "dump_single_json": True}
        for source_url in self.urls:
            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                result = ydl.extract_info(source_url, download=False)

            if "entries" in result:
                for entry in result["entries"]:
                    if not entry:
                        continue
                    entry_url = resolve_entry_url(entry)
                    if entry_url:
                        targets.append(entry_url)
            else:
                targets.append(normalize_download_url(source_url))
        return targets

    def run(self):
        all_downloaded, all_skipped, all_failed = [], [], []
        try:
            targets = self._collect_targets()
            self.total_items = len(targets)
            if self.total_items == 0:
                raise RuntimeError("No downloadable items found from provided URLs.")

            for item_url in targets:
                item_url = normalize_download_url(item_url)
                try:
                    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                        info = ydl.extract_info(item_url, download=False)

                    output_path, final_name = self._get_final_target(info)
                    display_title = info.get("title", final_name)
                    self.current_item.emit(f"Downloading: {display_title}")

                    with yt_dlp.YoutubeDL(self._build_ydl_opts(output_path)) as ydl:
                        ydl.download([item_url])

                    self.finished_items += 1
                    self._emit_overall_progress(100.0)
                    all_downloaded.append(
                        {
                            "source_url": item_url,
                            "title": display_title,
                            "filename": final_name,
                            "path": str(Path(output_path).resolve()),
                        }
                    )
                except DownloadError:
                    self.finished_items += 1
                    self._emit_overall_progress(100.0)
                    all_skipped.append(item_url)
                except Exception as exc:
                    self.finished_items += 1
                    self._emit_overall_progress(100.0)
                    all_failed.append({"url": item_url, "error": str(exc)})

            self.done.emit({"downloaded": all_downloaded, "skipped": all_skipped, "failed": all_failed})
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    THEMES = {
        "Midnight": {"bg": "#0f172a", "panel": "#111c33", "panel_soft": "#16223f", "text": "#e5ebf8", "accent": "#4f8cff"},
        "Graphite": {"bg": "#171717", "panel": "#232323", "panel_soft": "#2d2d2d", "text": "#f0f0f0", "accent": "#5aa8ff"},
    }

    def __init__(self):
        super().__init__()
        self.worker = None
        self.file_type = "audio"
        self.quality = "320"
        self.theme_name = "Midnight"
        self.last_download_directory = str(Path.cwd())
        self.setWindowTitle(APP_NAME)
        self.resize(920, 640)
        self._build_ui()
        self.apply_theme(self.theme_name)
        self._refresh_format_quality_badges()
        self._start_ffmpeg_polling()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        page = QVBoxLayout(root)
        page.setContentsMargins(24, 20, 24, 20)
        page.setSpacing(14)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        format_box = QFrame()
        format_box.setObjectName("infoBadge")
        format_box.setMinimumWidth(160)
        format_layout = QVBoxLayout(format_box)
        format_layout.setContentsMargins(16, 12, 16, 12)
        format_layout.setSpacing(6)
        format_caption = QLabel("FORMAT")
        format_caption.setObjectName("badgeCaption")
        self.format_value_label = QLabel(self.file_type.capitalize())
        self.format_value_label.setObjectName("badgeValue")
        format_layout.addWidget(format_caption)
        format_layout.addWidget(self.format_value_label)
        top_bar.addWidget(format_box)

        quality_box = QFrame()
        quality_box.setObjectName("infoBadge")
        quality_box.setMinimumWidth(160)
        quality_layout = QVBoxLayout(quality_box)
        quality_layout.setContentsMargins(16, 12, 16, 12)
        quality_layout.setSpacing(6)
        quality_caption = QLabel("QUALITY")
        quality_caption.setObjectName("badgeCaption")
        self.quality_value_label = QLabel(f"{self.quality} kbps")
        self.quality_value_label.setObjectName("badgeValue")
        quality_layout.addWidget(quality_caption)
        quality_layout.addWidget(self.quality_value_label)
        top_bar.addWidget(quality_box)

        top_bar.addStretch()

        settings_btn = QPushButton("⚙")
        settings_btn.setObjectName("gearButton")
        settings_btn.setFixedSize(42, 36)
        self.settings_menu = QMenu(self)
        self._init_settings_menu()
        settings_btn.clicked.connect(self._open_settings_menu)
        top_bar.addWidget(settings_btn)
        page.addLayout(top_bar)

        downloads_card = QFrame()
        downloads_layout = QVBoxLayout(downloads_card)
        downloads_layout.setContentsMargins(16, 16, 16, 16)
        downloads_layout.setSpacing(10)

        downloads_title = QLabel("Downloads")
        downloads_title.setFont(QFont("Segoe UI", 13, QFont.DemiBold))
        downloads_layout.addWidget(downloads_title)

        self.url_input = QTextEdit()
        self.url_input.setPlaceholderText("Paste links here, one per line")
        self.url_input.setMinimumHeight(300)
        downloads_layout.addWidget(self.url_input)

        actions = QHBoxLayout()
        self.start_btn = QPushButton("Start Download")
        self.start_btn.clicked.connect(self.start_download)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.url_input.clear)
        actions.addWidget(self.start_btn)
        actions.addWidget(clear_btn)
        actions.addStretch()
        downloads_layout.addLayout(actions)
        page.addWidget(downloads_card)

        progress_card = QFrame()
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(16, 14, 16, 14)
        progress_layout.setSpacing(8)

        self.current_item_label = QLabel("Ready")
        self.download_label = QLabel("Download progress: 0%")
        self.download_bar = QProgressBar()
        self.download_bar.setRange(0, 100)
        progress_layout.addWidget(self.current_item_label)
        progress_layout.addWidget(self.download_label)
        progress_layout.addWidget(self.download_bar)

        self.ffmpeg_label = QLabel("FFmpeg: waiting")
        self.ffmpeg_bar = QProgressBar()
        self.ffmpeg_bar.setRange(0, 100)
        progress_layout.addWidget(self.ffmpeg_label)
        progress_layout.addWidget(self.ffmpeg_bar)
        page.addWidget(progress_card)

    def _init_settings_menu(self):
        self.settings_menu.clear()

        type_menu = self.settings_menu.addMenu("Download Type")
        for label in ("audio", "video"):
            action = QAction(label.capitalize(), self)
            action.setCheckable(True)
            action.setChecked(self.file_type == label)
            action.triggered.connect(lambda checked, v=label: self._set_file_type(v))
            type_menu.addAction(action)

        quality_menu = self.settings_menu.addMenu("Audio Quality")
        for value in ("128", "192", "256", "320"):
            action = QAction(f"{value} kbps", self)
            action.setCheckable(True)
            action.setChecked(self.quality == value)
            action.triggered.connect(lambda checked, v=value: self._set_quality(v))
            quality_menu.addAction(action)

        theme_menu = self.settings_menu.addMenu("Theme")
        for name in self.THEMES.keys():
            action = QAction(name, self)
            action.setCheckable(True)
            action.setChecked(self.theme_name == name)
            action.triggered.connect(lambda checked, v=name: self._set_theme(v))
            theme_menu.addAction(action)

    def _open_settings_menu(self):
        self._init_settings_menu()
        sender = self.sender()
        self.settings_menu.exec(sender.mapToGlobal(sender.rect().bottomLeft()))

    def _set_file_type(self, value):
        self.file_type = value
        self._refresh_format_quality_badges()

    def _set_quality(self, value):
        self.quality = value
        self._refresh_format_quality_badges()

    def _set_theme(self, name):
        self.theme_name = name
        self.apply_theme(name)

    def _refresh_format_quality_badges(self):
        self.format_value_label.setText(self.file_type.capitalize())
        self.quality_value_label.setText(f"{self.quality} kbps")

    def _start_ffmpeg_polling(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_ffmpeg_status)
        self.timer.start(500)

    def refresh_ffmpeg_status(self):
        message, progress = get_ffmpeg_state()
        self.ffmpeg_label.setText(message)
        self.ffmpeg_bar.setValue(int(progress * 100))

    def start_download(self):
        urls = [u.strip() for u in self.url_input.toPlainText().splitlines() if u.strip()]
        if not urls:
            QMessageBox.warning(self, "Missing URLs", "Please add at least one URL.")
            return

        selected_directory = QFileDialog.getExistingDirectory(
            self,
            "Select Download Folder",
            self.last_download_directory,
        )
        if not selected_directory:
            return
        self.last_download_directory = selected_directory

        self.start_btn.setEnabled(False)
        self.download_bar.setValue(0)
        self.download_label.setText("Download progress: 0%")
        self.current_item_label.setText("Preparing download list...")

        self.worker = DownloadWorker(urls, self.file_type, self.quality, selected_directory)
        self.worker.progress.connect(self.on_progress)
        self.worker.current_item.connect(self.on_current_item)
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_current_item(self, text):
        self.current_item_label.setText(text)

    def on_progress(self, value):
        self.download_bar.setValue(value)
        self.download_label.setText(f"Download progress: {value}%")

    def on_done(self, result):
        self.start_btn.setEnabled(True)
        self.current_item_label.setText("Finished")
        QMessageBox.information(
            self,
            "Finished",
            f"Downloaded: {len(result.get('downloaded', []))}\n"
            f"Skipped: {len(result.get('skipped', []))}\n"
            f"Failed: {len(result.get('failed', []))}",
        )

    def on_failed(self, error):
        self.start_btn.setEnabled(True)
        self.current_item_label.setText("Error")
        QMessageBox.critical(self, "Download Error", error)

    def apply_theme(self, name):
        theme = self.THEMES.get(name, self.THEMES["Midnight"])
        self.setStyleSheet(
            f"""
            QMainWindow {{
                background: {theme['bg']};
                color: {theme['text']};
                font-family: Segoe UI;
                font-size: 13px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                padding: 0px;
            }}
            QFrame {{
                background: {theme['panel']};
                border: 1px solid {theme['panel_soft']};
                border-radius: 12px;
            }}
            QFrame#infoBadge {{
                background-color: {theme['panel_soft']};
                border: 1px solid rgba(79, 140, 255, 0.35);
                border-radius: 10px;
                min-height: 58px;
            }}
            QFrame#infoBadge QLabel {{
                background: transparent;
            }}
            QLabel#badgeCaption {{
                color: #94a3b8;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 0.1em;
                padding-bottom: 2px;
            }}
            QLabel#badgeValue {{
                color: {theme['text']};
                font-size: 15px;
                font-weight: 600;
                padding-top: 0px;
            }}
            QTextEdit {{
                background: {theme['panel_soft']};
                border: 1px solid #314267;
                border-radius: 10px;
                padding: 10px;
            }}
            QPushButton {{
                background: {theme['accent']};
                color: #ffffff;
                border: none;
                border-radius: 10px;
                padding: 9px 14px;
                font-weight: 600;
            }}
            QPushButton#gearButton {{
                font-size: 18px;
                padding: 0;
            }}
            QPushButton:disabled {{
                background: #6b7280;
                color: #e5e7eb;
            }}
            QMenu {{
                background: {theme['panel_soft']};
                color: {theme['text']};
                border: 1px solid #314267;
                padding: 6px;
            }}
            QMenu::item {{
                padding: 6px 24px 6px 12px;
                border-radius: 6px;
            }}
            QMenu::item:selected {{
                background: {theme['accent']};
            }}
            QProgressBar {{
                border: 1px solid #314267;
                border-radius: 8px;
                background: {theme['panel_soft']};
                text-align: center;
            }}
            QProgressBar::chunk {{
                background: {theme['accent']};
                border-radius: 7px;
            }}
            """
        )


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
