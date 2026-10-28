#!/usr/bin/env python3
"""
gui.py — Modern GUI for Spotify Downloader
Built with CustomTkinter. Provides a clean, simple interface to download
Spotify songs/playlists with full metadata, album art, and progress tracking.
"""

import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

# Ensure stdout/stderr are safe on Windows even under pythonw.exe
if sys.stdout is None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass
if sys.stderr is None:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

if sys.platform == "win32":
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.stderr is not None:
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import customtkinter as ctk

# Ensure the local directory is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from spotify_dl import (
    run_download,
    extract_spotify_id,
    batch_transcode_folder,
    analyze_folder_audio,
    get_reducible_bitrate_options,
    fix_library_album_tags,
)


class SpotifyDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window Appearance
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")

        self.title("Spotify Downloader")
        self.geometry("660x650")
        self.minsize(620, 580)

        # Set window icon if available
        icon_path = SCRIPT_DIR / "icon.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        # State variables
        self.is_downloading = False
        self._cancel_requested = False
        self._download_thread: threading.Thread = None

        # Load saved configuration
        self.config_file = SCRIPT_DIR / ".gui_config.json"
        self.saved_config = self._load_config()
        self.output_dir = Path(self.saved_config.get("output_dir", str(Path.home() / "Music" / "Spotify Downloads")))

        self._create_widgets()

    def _load_config(self) -> dict:
        default_dir = Path.home() / "Music" / "Spotify Downloads"
        defaults = {
            "output_dir": str(default_dir),
            "format": "mp3",
            "bitrate": "320k",
            "threads": "4 Songs at once (Fast)",
            "limit": "",
            "force": False,
        }
        if self.config_file.exists():
            try:
                import json
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        defaults.update(data)
            except Exception:
                pass
        return defaults

    def _save_config(self):
        try:
            import json
            folder_val = self.folder_entry.get().strip() if hasattr(self, "folder_entry") else ""
            fmt_val = self.format_var.get().strip() if hasattr(self, "format_var") else "mp3"
            br_val = self.bitrate_var.get().strip() if hasattr(self, "bitrate_var") else "320k"
            th_val = self.threads_var.get() if hasattr(self, "threads_var") else "4 Songs at once (Fast)"
            lim_val = self.limit_entry.get().strip() if hasattr(self, "limit_entry") else ""
            force_val = self.force_var.get() if hasattr(self, "force_var") else False

            cfg = {
                "output_dir": folder_val or str(Path.home() / "Music" / "Spotify Downloads"),
                "format": fmt_val,
                "bitrate": br_val,
                "threads": th_val,
                "limit": lim_val,
                "force": force_val,
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def _create_widgets(self):
        # Main padding container
        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=24, pady=18)

        # Header Title
        title_label = ctk.CTkLabel(
            main_frame,
            text="Spotify Downloader",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#1DB954",
        )
        title_label.pack(anchor="w", pady=(0, 2))

        subtitle_label = ctk.CTkLabel(
            main_frame,
            text="Download songs, albums, and playlists with album art & tags",
            font=ctk.CTkFont(size=12),
            text_color="gray70",
        )
        subtitle_label.pack(anchor="w", pady=(0, 14))

        # ── Link Input Card ──────────────────────────────────────────────────
        link_frame = ctk.CTkFrame(main_frame, fg_color="#1E1E1E", corner_radius=10)
        link_frame.pack(fill="x", pady=(0, 10), padx=0)

        link_inner = ctk.CTkFrame(link_frame, fg_color="transparent")
        link_inner.pack(fill="x", padx=14, pady=12)

        link_title = ctk.CTkLabel(
            link_inner,
            text="Spotify Link (Playlist, Album, or Track):",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        link_title.pack(anchor="w", pady=(0, 6))

        link_row = ctk.CTkFrame(link_inner, fg_color="transparent")
        link_row.pack(fill="x")

        self.url_entry = ctk.CTkEntry(
            link_row,
            placeholder_text="https://open.spotify.com/playlist/... or track link",
            font=ctk.CTkFont(size=12),
            height=36,
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        paste_btn = ctk.CTkButton(
            link_row,
            text="Paste",
            width=65,
            height=36,
            fg_color="#333333",
            hover_color="#444444",
            command=self._paste_clipboard,
        )
        paste_btn.pack(side="left", padx=(0, 4))

        clear_btn = ctk.CTkButton(
            link_row,
            text="Clear",
            width=55,
            height=36,
            fg_color="#333333",
            hover_color="#444444",
            command=lambda: self.url_entry.delete(0, "end"),
        )
        clear_btn.pack(side="left")

        # ── Output Directory Card ────────────────────────────────────────────
        folder_frame = ctk.CTkFrame(main_frame, fg_color="#1E1E1E", corner_radius=10)
        folder_frame.pack(fill="x", pady=(0, 14), padx=0)

        folder_inner = ctk.CTkFrame(folder_frame, fg_color="transparent")
        folder_inner.pack(fill="x", padx=14, pady=12)

        folder_title = ctk.CTkLabel(
            folder_inner,
            text="Download Folder:",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        folder_title.pack(anchor="w", pady=(0, 6))

        folder_row = ctk.CTkFrame(folder_inner, fg_color="transparent")
        folder_row.pack(fill="x")

        self.folder_entry = ctk.CTkEntry(
            folder_row,
            font=ctk.CTkFont(size=12),
            height=36,
        )
        self.folder_entry.insert(0, str(self.output_dir))
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        browse_btn = ctk.CTkButton(
            folder_row,
            text="Browse...",
            width=80,
            height=36,
            fg_color="#333333",
            hover_color="#444444",
            command=self._browse_folder,
        )
        browse_btn.pack(side="left", padx=(0, 4))

        open_folder_btn = ctk.CTkButton(
            folder_row,
            text="Open",
            width=60,
            height=36,
            fg_color="#333333",
            hover_color="#444444",
            command=self._open_in_explorer,
        )
        open_folder_btn.pack(side="left")

        # Tool button for downsizing / converting quality of songs in folder
        folder_tools_row = ctk.CTkFrame(folder_inner, fg_color="transparent")
        folder_tools_row.pack(fill="x", pady=(8, 0))

        downsize_tool_btn = ctk.CTkButton(
            folder_tools_row,
            text="Downsize Quality...",
            font=ctk.CTkFont(size=11, weight="bold"),
            height=32,
            fg_color="#1E3A5F",
            hover_color="#2A5282",
            text_color="#64B5F6",
            command=self._open_downsize_dialog,
        )
        downsize_tool_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        fix_tags_btn = ctk.CTkButton(
            folder_tools_row,
            text="Fix Split Albums (Apple Music)...",
            font=ctk.CTkFont(size=11, weight="bold"),
            height=32,
            fg_color="#2D2338",
            hover_color="#453457",
            text_color="#CE93D8",
            command=self._open_fix_tags_dialog,
        )
        fix_tags_btn.pack(side="left", fill="x", expand=True)

        # ── Download Settings Card ───────────────────────────────────────────
        settings_frame = ctk.CTkFrame(main_frame, fg_color="#1E1E1E", corner_radius=10)
        settings_frame.pack(fill="x", pady=(0, 14), padx=0)

        settings_inner = ctk.CTkFrame(settings_frame, fg_color="transparent")
        settings_inner.pack(fill="x", padx=14, pady=12)

        settings_title = ctk.CTkLabel(
            settings_inner,
            text="Download Settings:",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        settings_title.pack(anchor="w", pady=(0, 8))

        # Row 1: Format, Bitrate, Speed
        row1 = ctk.CTkFrame(settings_inner, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 10))

        fmt_frame = ctk.CTkFrame(row1, fg_color="transparent")
        fmt_frame.pack(side="left", padx=(0, 16))
        fmt_label = ctk.CTkLabel(fmt_frame, text="Format:", font=ctk.CTkFont(size=12, weight="bold"), text_color="gray85")
        fmt_label.pack(anchor="w", pady=(0, 3))
        self.format_var = ctk.StringVar(value=self.saved_config.get("format", "mp3"))
        self.format_menu = ctk.CTkOptionMenu(
            fmt_frame,
            values=["mp3", "m4a", "flac", "opus", "wav"],
            variable=self.format_var,
            width=90,
            height=32,
            fg_color="#2A2A2A",
            button_color="#333333",
            button_hover_color="#444444",
        )
        self.format_menu.pack(side="left")

        bitrate_frame = ctk.CTkFrame(row1, fg_color="transparent")
        bitrate_frame.pack(side="left", padx=(0, 16))
        bitrate_label = ctk.CTkLabel(bitrate_frame, text="Bitrate:", font=ctk.CTkFont(size=12, weight="bold"), text_color="gray85")
        bitrate_label.pack(anchor="w", pady=(0, 3))
        self.bitrate_var = ctk.StringVar(value=self.saved_config.get("bitrate", "320k"))
        self.bitrate_menu = ctk.CTkOptionMenu(
            bitrate_frame,
            values=["320k", "256k", "192k", "128k"],
            variable=self.bitrate_var,
            width=95,
            height=32,
            fg_color="#2A2A2A",
            button_color="#333333",
            button_hover_color="#444444",
        )
        self.bitrate_menu.pack(side="left")

        speed_frame = ctk.CTkFrame(row1, fg_color="transparent")
        speed_frame.pack(side="left", fill="x", expand=True)
        speed_label = ctk.CTkLabel(speed_frame, text="Parallel Speed:", font=ctk.CTkFont(size=12, weight="bold"), text_color="gray85")
        speed_label.pack(anchor="w", pady=(0, 3))
        self.threads_var = ctk.StringVar(value=self.saved_config.get("threads", "4 Songs at once (Fast)"))
        self.threads_menu = ctk.CTkOptionMenu(
            speed_frame,
            values=[
                "1 Song at a time",
                "3 Songs at once",
                "4 Songs at once (Fast)",
                "6 Songs at once (Turbo)",
            ],
            variable=self.threads_var,
            width=190,
            height=32,
            fg_color="#2A2A2A",
            button_color="#333333",
            button_hover_color="#444444",
        )
        self.threads_menu.pack(anchor="w")

        # Row 2: Limit & Overwrite checkbox
        row2 = ctk.CTkFrame(settings_inner, fg_color="transparent")
        row2.pack(fill="x")

        limit_frame = ctk.CTkFrame(row2, fg_color="transparent")
        limit_frame.pack(side="left", padx=(0, 24))
        limit_label = ctk.CTkLabel(limit_frame, text="Track Limit:", font=ctk.CTkFont(size=12, weight="bold"), text_color="gray85")
        limit_label.pack(side="left", padx=(0, 8))
        self.limit_entry = ctk.CTkEntry(
            limit_frame,
            placeholder_text="All tracks",
            font=ctk.CTkFont(size=12),
            width=100,
            height=32,
        )
        saved_limit = self.saved_config.get("limit", "")
        if saved_limit:
            self.limit_entry.insert(0, str(saved_limit))
        self.limit_entry.pack(side="left")

        self.force_var = ctk.BooleanVar(value=self.saved_config.get("force", False))
        self.force_checkbox = ctk.CTkCheckBox(
            row2,
            text="Overwrite existing tracks (force re-download)",
            variable=self.force_var,
            font=ctk.CTkFont(size=12),
            checkmark_color="#000000",
            fg_color="#1DB954",
            hover_color="#1AA34A",
            height=32,
        )
        self.force_checkbox.pack(side="left")

        # ── Download Action Button ───────────────────────────────────────────
        self.start_btn = ctk.CTkButton(
            main_frame,
            text="▶ Start Download",
            font=ctk.CTkFont(size=15, weight="bold"),
            height=42,
            fg_color="#1DB954",
            hover_color="#1AA34A",
            text_color="#000000",
            command=self._on_start_click,
        )
        self.start_btn.pack(fill="x", pady=(0, 14))

        # ── Progress & Status Section ────────────────────────────────────────
        progress_card = ctk.CTkFrame(main_frame, fg_color="#1E1E1E", corner_radius=10)
        progress_card.pack(fill="both", expand=True)

        progress_inner = ctk.CTkFrame(progress_card, fg_color="transparent")
        progress_inner.pack(fill="both", expand=True, padx=14, pady=12)

        status_header_row = ctk.CTkFrame(progress_inner, fg_color="transparent")
        status_header_row.pack(fill="x", pady=(0, 4))

        self.status_label = ctk.CTkLabel(
            status_header_row,
            text="Ready to download",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        self.percent_label = ctk.CTkLabel(
            status_header_row,
            text="0%",
            font=ctk.CTkFont(size=12),
            text_color="gray70",
        )
        self.percent_label.pack(side="right")

        self.progress_bar = ctk.CTkProgressBar(
            progress_inner,
            height=10,
            progress_color="#1DB954",
            fg_color="#333333",
        )
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", pady=(2, 10))

        # Log box for live details
        self.log_textbox = ctk.CTkTextbox(
            progress_inner,
            height=100,
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#121212",
            text_color="gray85",
            corner_radius=6,
        )
        self.log_textbox.pack(fill="both", expand=True)
        self.log_textbox.insert("end", "Paste a Spotify link above and click 'Start Download'.\n")
        self.log_textbox.configure(state="disabled")

    def _paste_clipboard(self):
        try:
            clipboard_text = self.clipboard_get().strip()
            if clipboard_text:
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, clipboard_text)
        except Exception:
            pass

    def _browse_folder(self):
        current = self.folder_entry.get().strip()
        chosen = filedialog.askdirectory(initialdir=current or str(Path.home()))
        if chosen:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, chosen)
            self._save_config()

    def _open_in_explorer(self):
        path_str = self.folder_entry.get().strip()
        p = Path(path_str)
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))

    def _open_downsize_dialog(self):
        folder_str = self.folder_entry.get().strip()
        f_path = Path(folder_str) if folder_str else self.output_dir
        dialog = DownsizeDialog(self, f_path)
        dialog.focus()

    def _open_fix_tags_dialog(self):
        folder_str = self.folder_entry.get().strip()
        f_path = Path(folder_str) if folder_str else self.output_dir
        dialog = FixAlbumTagsDialog(self, f_path)
        dialog.focus()

    def _append_log(self, text: str):
        self.log_textbox.configure(state="normal")
        self.log_textbox.insert("end", f"{text}\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")

    def _on_start_click(self):
        if self.is_downloading:
            # User clicked Cancel
            self._cancel_requested = True
            self.status_label.configure(text="Cancelling download...")
            self.start_btn.configure(state="disabled", text="Stopping...")
            return

        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Missing Link", "Please paste a Spotify playlist, album, or track link first.")
            return

        # Auto-add https:// if missing
        if not url.startswith("http://") and not url.startswith("https://") and not url.startswith("spotify:"):
            url = "https://" + url
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, url)

        try:
            extract_spotify_id(url)
        except ValueError:
            messagebox.showerror(
                "Invalid Link",
                "The link does not appear to be a valid Spotify URL.\nExample: https://open.spotify.com/playlist/..."
            )
            return

        folder_str = self.folder_entry.get().strip()
        if not folder_str:
            folder_str = str(Path.home() / "Music" / "Spotify Downloads")

        output_path = Path(folder_str).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)

        # Validate limit if entered
        limit_str = self.limit_entry.get().strip()
        limit_val = None
        if limit_str:
            if limit_str.isdigit() and int(limit_str) > 0:
                limit_val = int(limit_str)
            else:
                messagebox.showwarning(
                    "Invalid Limit",
                    "Track limit must be a positive number (e.g. 20) or left empty for all tracks."
                )
                return

        fmt = self.format_var.get().strip().lower() or "mp3"
        bitrate = self.bitrate_var.get().strip() or "320k"
        force_redownload = self.force_var.get()

        # Determine max parallel workers from dropdown
        speed_text = self.threads_var.get()
        m = re.search(r"\d+", speed_text)
        max_workers = int(m.group()) if m else 4

        self._save_config()

        # Reset progress & start thread
        self.is_downloading = True
        self._cancel_requested = False
        self.progress_bar.set(0.0)
        self.percent_label.configure(text="0%")
        self.status_label.configure(text="Connecting to Spotify...")
        self.start_btn.configure(
            text="⏹ Cancel Download",
            fg_color="#E53935",
            hover_color="#C62828",
            text_color="#FFFFFF",
        )

        self._append_log("─" * 45)
        self._append_log(f"Starting download to: {output_path}")
        settings_summary = f"Format: {fmt.upper()} @ {bitrate} | Speed: {max_workers} songs at once"
        if limit_val:
            settings_summary += f" | Limit: first {limit_val} tracks"
        if force_redownload:
            settings_summary += " | Force overwrite: ON"
        self._append_log(settings_summary)

        self._download_thread = threading.Thread(
            target=self._run_download_worker,
            args=(url, output_path, max_workers, fmt, bitrate, limit_val, force_redownload),
            daemon=True,
        )
        self._download_thread.start()

    def _run_download_worker(
        self,
        url: str,
        output_path: Path,
        max_workers: int = 4,
        fmt: str = "mp3",
        bitrate: str = "320k",
        limit: Optional[int] = None,
        force_redownload: bool = False,
    ):
        def progress_callback(status: str, prog: float, current_track: str = None):
            self.after(0, lambda s=status, p=prog, c=current_track: self._update_progress(s, p, c))

        def cancel_check() -> bool:
            return self._cancel_requested

        try:
            result = run_download(
                playlist_url=url,
                output_dir=output_path,
                fmt=fmt,
                bitrate=bitrate,
                limit=limit,
                force_redownload=force_redownload,
                max_workers=max_workers,
                callback=progress_callback,
                cancel_check=cancel_check,
            )
            self.after(0, lambda r=result: self._on_download_complete(r))
        except Exception as e:
            import traceback
            err_details = traceback.format_exc()
            self.after(0, lambda m=str(e), d=err_details: self._on_download_error(m, d))

    def _update_progress(self, status: str, prog: float, current_track: str = None):
        try:
            self.status_label.configure(text=status)
            if prog >= 0.0:
                self.progress_bar.set(min(1.0, max(0.0, prog)))
                pct = int(min(1.0, max(0.0, prog)) * 100)
                self.percent_label.configure(text=f"{pct}%")
            self._append_log(status)
        except Exception:
            pass

    def _on_download_complete(self, result: dict):
        self.is_downloading = False
        self.start_btn.configure(
            state="normal",
            text="▶ Start Download",
            fg_color="#1DB954",
            hover_color="#1AA34A",
            text_color="#000000",
        )

        if result.get("cancelled"):
            self.status_label.configure(text="Download cancelled.")
            self._append_log("Download was cancelled.")
            return

        downloaded = result.get("downloaded", 0)
        skipped = result.get("skipped", 0)
        failed = result.get("failed", 0)

        summary_msg = f"Done! {downloaded} downloaded, {skipped} already up-to-date."
        if failed > 0:
            summary_msg += f" ({failed} failed)"

        self.status_label.configure(text=summary_msg)
        self.progress_bar.set(1.0)
        self.percent_label.configure(text="100%")
        self._append_log(f"✔ {summary_msg}")

        # Show friendly dialog
        messagebox.showinfo(
            "Download Finished",
            f"{summary_msg}\n\nFiles saved in:\n{result.get('output_dir', '')}"
        )

    def _on_download_error(self, error_msg: str, error_details: str = ""):
        self.is_downloading = False
        self.start_btn.configure(
            state="normal",
            text="▶ Start Download",
            fg_color="#1DB954",
            hover_color="#1AA34A",
            text_color="#000000",
        )
        self.status_label.configure(text=f"Error: {error_msg}")
        self._append_log(f"ERROR: {error_msg}")
        if error_details:
            self._append_log(error_details)
        messagebox.showerror("Download Error", f"An error occurred while downloading:\n\n{error_msg}")


class DownsizeDialog(ctk.CTkToplevel):
    def __init__(self, parent, default_folder: Path):
        super().__init__(parent)

        self.title("Downsize & Adjust Audio Quality")
        self.geometry("640x670")
        self.minsize(580, 580)
        self.transient(parent)

        icon_path = SCRIPT_DIR / "icon.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.is_processing = False
        self._cancel_requested = False
        self._closed = False
        self._msg_queue = queue.Queue()
        self._work_thread: Optional[threading.Thread] = None
        self.selected_folder = Path(default_folder).expanduser().resolve()

        self._create_widgets()
        self._poll_queue()
        self._scan_folder_stats()

    def _poll_queue(self):
        if self._closed:
            return
        try:
            while not self._msg_queue.empty():
                m_type, payload = self._msg_queue.get_nowait()
                if m_type == "scan":
                    stats, text, color = payload
                    self._update_scan_ui(stats, text, color)
                elif m_type == "progress":
                    status, prog = payload
                    self._update_progress(status, prog)
                elif m_type == "complete":
                    self._on_downsize_complete(payload)
                elif m_type == "error":
                    self._on_downsize_error(payload)
        except Exception:
            pass
        if not self._closed:
            self.after(50, self._poll_queue)

    def _create_widgets(self):
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=16)

        # Header
        title = ctk.CTkLabel(
            container,
            text="Downsize & Adjust Audio Quality",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#64B5F6",
        )
        title.pack(anchor="w", pady=(0, 2))

        sub = ctk.CTkLabel(
            container,
            text="Reduce file sizes or adjust bitrates for all music in a folder (ID3 tags & cover art preserved)",
            font=ctk.CTkFont(size=11),
            text_color="gray70",
        )
        sub.pack(anchor="w", pady=(0, 12))

        # Folder selection card
        folder_card = ctk.CTkFrame(container, fg_color="#1E1E1E", corner_radius=10)
        folder_card.pack(fill="x", pady=(0, 10))
        folder_inner = ctk.CTkFrame(folder_card, fg_color="transparent")
        folder_inner.pack(fill="x", padx=12, pady=10)

        f_label = ctk.CTkLabel(
            folder_inner,
            text="Folder with Music:",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        f_label.pack(anchor="w", pady=(0, 4))

        f_row = ctk.CTkFrame(folder_inner, fg_color="transparent")
        f_row.pack(fill="x")

        self.folder_entry = ctk.CTkEntry(f_row, font=ctk.CTkFont(size=12), height=34)
        self.folder_entry.insert(0, str(self.selected_folder))
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        browse_btn = ctk.CTkButton(
            f_row,
            text="Browse...",
            width=75,
            height=34,
            fg_color="#333333",
            hover_color="#444444",
            command=self._browse_folder,
        )
        browse_btn.pack(side="left")

        self.folder_stats_label = ctk.CTkLabel(
            folder_inner,
            text="Scanning folder for music...",
            font=ctk.CTkFont(size=11),
            text_color="#81C784",
        )
        self.folder_stats_label.pack(anchor="w", pady=(6, 0))

        # Settings card
        settings_card = ctk.CTkFrame(container, fg_color="#1E1E1E", corner_radius=10)
        settings_card.pack(fill="x", pady=(0, 10))
        settings_inner = ctk.CTkFrame(settings_card, fg_color="transparent")
        settings_inner.pack(fill="x", padx=12, pady=10)

        s_title = ctk.CTkLabel(
            settings_inner,
            text="Target Quality & Settings:",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        s_title.pack(anchor="w", pady=(0, 8))

        row1 = ctk.CTkFrame(settings_inner, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 8))

        # Target Bitrate
        br_frame = ctk.CTkFrame(row1, fg_color="transparent")
        br_frame.pack(side="left", fill="x", expand=True, padx=(0, 10))
        br_label = ctk.CTkLabel(br_frame, text="Target Bitrate (Downsize to):", font=ctk.CTkFont(size=11, weight="bold"), text_color="gray85")
        br_label.pack(anchor="w", pady=(0, 2))
        self.bitrate_var = ctk.StringVar(value="Analyzing folder...")
        self.bitrate_menu = ctk.CTkOptionMenu(
            br_frame,
            values=["Analyzing folder..."],
            variable=self.bitrate_var,
            height=32,
            fg_color="#2A2A2A",
            button_color="#333333",
            button_hover_color="#444444",
        )
        self.bitrate_menu.pack(fill="x")
        self.savings_hint_label = ctk.CTkLabel(
            br_frame,
            text="Analyzing audio files in folder...",
            font=ctk.CTkFont(size=10),
            text_color="gray60",
            anchor="w",
        )
        self.savings_hint_label.pack(anchor="w", pady=(3, 0))

        # Speed / Workers
        speed_frame = ctk.CTkFrame(row1, fg_color="transparent")
        speed_frame.pack(side="left", fill="x", expand=True)
        speed_label = ctk.CTkLabel(speed_frame, text="Processing Speed:", font=ctk.CTkFont(size=11, weight="bold"), text_color="gray85")
        speed_label.pack(anchor="w", pady=(0, 2))
        self.speed_var = ctk.StringVar(value="4 Songs at once (Fast)")
        self.speed_menu = ctk.CTkOptionMenu(
            speed_frame,
            values=[
                "1 Song at a time",
                "2 Songs at once",
                "4 Songs at once (Fast)",
                "6 Songs at once (Turbo)",
            ],
            variable=self.speed_var,
            height=32,
            fg_color="#2A2A2A",
            button_color="#333333",
            button_hover_color="#444444",
        )
        self.speed_menu.pack(fill="x")

        row2 = ctk.CTkFrame(settings_inner, fg_color="transparent")
        row2.pack(fill="x", pady=(0, 4))

        # Output Mode
        mode_frame = ctk.CTkFrame(row2, fg_color="transparent")
        mode_frame.pack(side="left", fill="x", expand=True, padx=(0, 10))
        mode_label = ctk.CTkLabel(mode_frame, text="Output Mode:", font=ctk.CTkFont(size=11, weight="bold"), text_color="gray85")
        mode_label.pack(anchor="w", pady=(0, 2))
        self.mode_var = ctk.StringVar(value="Replace files in-place (frees space immediately)")
        self.mode_menu = ctk.CTkOptionMenu(
            mode_frame,
            values=[
                "Replace files in-place (frees space immediately)",
                "Save to new subfolder",
            ],
            variable=self.mode_var,
            height=32,
            fg_color="#2A2A2A",
            button_color="#333333",
            button_hover_color="#444444",
        )
        self.mode_menu.pack(fill="x")

        # Format
        fmt_frame = ctk.CTkFrame(row2, fg_color="transparent")
        fmt_frame.pack(side="left", fill="x", expand=True)
        fmt_label = ctk.CTkLabel(fmt_frame, text="Output Format:", font=ctk.CTkFont(size=11, weight="bold"), text_color="gray85")
        fmt_label.pack(anchor="w", pady=(0, 2))
        self.fmt_var = ctk.StringVar(value="Keep original format")
        self.fmt_menu = ctk.CTkOptionMenu(
            fmt_frame,
            values=[
                "Keep original format",
                "mp3",
                "m4a",
                "opus",
                "flac",
            ],
            variable=self.fmt_var,
            height=32,
            fg_color="#2A2A2A",
            button_color="#333333",
            button_hover_color="#444444",
        )
        self.fmt_menu.pack(fill="x")

        # Note on audio upgrade
        tip_label = ctk.CTkLabel(
            settings_inner,
            text="Tip: To truly upgrade lower-bitrate songs to 320k, enable 'Overwrite existing tracks' on the main window to re-download the original Spotify source streams.",
            font=ctk.CTkFont(size=10),
            text_color="gray55",
            wraplength=560,
            justify="left",
        )
        tip_label.pack(anchor="w", pady=(6, 0))

        # Action Buttons
        btn_row = ctk.CTkFrame(container, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 10))

        self.start_btn = ctk.CTkButton(
            btn_row,
            text="Start Downsizing",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=38,
            fg_color="#1DB954",
            hover_color="#1AA34A",
            text_color="#000000",
            command=self._on_start_click,
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.close_btn = ctk.CTkButton(
            btn_row,
            text="Close",
            width=80,
            height=38,
            fg_color="#333333",
            hover_color="#444444",
            command=self._on_close,
        )
        self.close_btn.pack(side="left")

        # Progress & Results Card
        progress_card = ctk.CTkFrame(container, fg_color="#1E1E1E", corner_radius=10)
        progress_card.pack(fill="both", expand=True)
        progress_inner = ctk.CTkFrame(progress_card, fg_color="transparent")
        progress_inner.pack(fill="both", expand=True, padx=12, pady=10)

        header_row = ctk.CTkFrame(progress_inner, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 2))

        self.status_label = ctk.CTkLabel(
            header_row,
            text="Ready",
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        self.percent_label = ctk.CTkLabel(
            header_row,
            text="0%",
            font=ctk.CTkFont(size=11),
            text_color="gray70",
        )
        self.percent_label.pack(side="right")

        self.progress_bar = ctk.CTkProgressBar(
            progress_inner,
            height=8,
            progress_color="#64B5F6",
            fg_color="#333333",
        )
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", pady=(2, 6))

        self.log_box = ctk.CTkTextbox(
            progress_inner,
            height=110,
            font=ctk.CTkFont(family="Consolas", size=10),
            fg_color="#121212",
            text_color="gray85",
            corner_radius=6,
        )
        self.log_box.pack(fill="both", expand=True)
        self.log_box.insert("end", "Select your target bitrate and click 'Start Downsizing'.\n")
        self.log_box.configure(state="disabled")

    def _browse_folder(self):
        if self.is_processing:
            return
        chosen = filedialog.askdirectory(initialdir=str(self.selected_folder))
        if chosen:
            self.selected_folder = Path(chosen)
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, str(self.selected_folder))
            self._scan_folder_stats()

    def _scan_folder_stats(self):
        self.folder_stats_label.configure(text="Analyzing folder & audio bitrates...", text_color="#81C784")
        self.bitrate_menu.configure(values=["Scanning audio files..."])
        self.bitrate_var.set("Scanning audio files...")
        self.start_btn.configure(state="disabled")

        folder_str = self.folder_entry.get().strip() if hasattr(self, "folder_entry") else str(self.selected_folder)
        f_path = Path(folder_str).expanduser().resolve()

        def _worker(target_path: Path):
            if not target_path.exists():
                self._msg_queue.put(("scan", (None, "Folder does not exist", "#E53935")))
                return

            stats = analyze_folder_audio(target_path)
            count = stats["total_files"]
            if count == 0:
                self._msg_queue.put(("scan", (stats, "No audio files found in this folder", "gray60")))
                return

            total_mb = stats["total_bytes"] / (1024 * 1024)
            size_str = f"{total_mb/1024:.2f} GB" if total_mb >= 1024 else f"{total_mb:.1f} MB"
            max_br = stats["max_bitrate"]
            min_br = stats["min_bitrate"]

            if stats["is_lossless"]:
                q_str = "Lossless (FLAC/WAV)"
            elif max_br == min_br:
                q_str = f"{max_br} kbps"
            else:
                q_str = f"{min_br}k - {max_br}k"

            text = f"Found {count} songs ({size_str}) | Current Quality: {q_str}"
            self._msg_queue.put(("scan", (stats, text, "#81C784")))

        threading.Thread(target=_worker, args=(f_path,), daemon=True).start()

    def _update_scan_ui(self, stats: Optional[dict], text: str, color: str):
        self.folder_stats_label.configure(text=text, text_color=color)
        if not stats or stats.get("total_files", 0) == 0:
            self.bitrate_menu.configure(values=["No audio files found"])
            self.bitrate_var.set("No audio files found")
            self.savings_hint_label.configure(text="Select a folder containing MP3, M4A, or FLAC songs.", text_color="gray60")
            self.start_btn.configure(state="disabled")
            return

        total_mb = stats["total_bytes"] / (1024 * 1024)
        max_br = stats["max_bitrate"]
        reducible_options = get_reducible_bitrate_options(max_br, total_mb)

        if not reducible_options:
            # Files already at lowest bitrate
            no_opt_msg = f"Already at minimum quality ({max_br} kbps)"
            self.bitrate_menu.configure(values=[no_opt_msg])
            self.bitrate_var.set(no_opt_msg)
            self.savings_hint_label.configure(text="All songs in this folder are already at minimum bitrate. No downsizing needed.", text_color="#E53935")
            self.start_btn.configure(state="disabled")
            self._append_log(f"Notice: All songs are already at or below {max_br} kbps. Further downsizing is not applicable.")
            return

        # Valid reducible options found
        labels = [opt[1] for opt in reducible_options]
        self.bitrate_menu.configure(values=labels)
        self.bitrate_var.set(labels[0])
        self.savings_hint_label.configure(
            text="Smart Filter: Showing only bitrates lower than your current songs to guarantee space savings.",
            text_color="#81C784"
        )
        self.start_btn.configure(state="normal")

    def _append_log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_start_click(self):
        if self.is_processing:
            self._cancel_requested = True
            self.status_label.configure(text="Cancelling...")
            self.start_btn.configure(state="disabled", text="Stopping...")
            return

        f_path = Path(self.folder_entry.get().strip()).expanduser().resolve()
        if not f_path.exists():
            messagebox.showerror("Invalid Folder", "Please choose a valid folder containing music.")
            return

        # Bitrate
        raw_br = self.bitrate_var.get()
        m = re.search(r"\d+k", raw_br)
        if not m:
            messagebox.showwarning(
                "No Reducible Bitrate",
                "Please select a valid reducible target bitrate from the dropdown."
            )
            return
        target_bitrate = m.group()

        # Format
        fmt_choice = self.fmt_var.get().lower()
        target_fmt = None if "original" in fmt_choice else fmt_choice

        # In place or subfolder
        in_place = "in-place" in self.mode_var.get()
        output_dir = None
        if not in_place:
            output_dir = f_path / f"Downsized_{target_bitrate}"
            output_dir.mkdir(parents=True, exist_ok=True)

        # Workers
        speed_str = self.speed_var.get()
        m_speed = re.search(r"\d+", speed_str)
        workers = int(m_speed.group()) if m_speed else 4

        self.is_processing = True
        self._cancel_requested = False
        self.progress_bar.set(0.0)
        self.percent_label.configure(text="0%")
        self.start_btn.configure(
            text="⏹ Cancel",
            fg_color="#E53935",
            hover_color="#C62828",
            text_color="#FFFFFF",
        )

        self._append_log("─" * 45)
        self._append_log(f"Starting downsizing: {f_path}")
        self._append_log(f"Target Bitrate: {target_bitrate} | Mode: {'In-place replacement' if in_place else str(output_dir)}")

        self._work_thread = threading.Thread(
            target=self._run_downsize_worker,
            args=(f_path, target_bitrate, target_fmt, in_place, output_dir, workers),
            daemon=True,
        )
        self._work_thread.start()

    def _run_downsize_worker(self, f_path: Path, target_bitrate: str, target_fmt: Optional[str], in_place: bool, output_dir: Optional[Path], workers: int):
        def _cb(status: str, prog: float, stats: dict):
            self._msg_queue.put(("progress", (status, prog)))

        def _cancel() -> bool:
            return self._cancel_requested

        try:
            res = batch_transcode_folder(
                folder=f_path,
                target_bitrate=target_bitrate,
                target_fmt=target_fmt,
                in_place=in_place,
                output_dir=output_dir,
                max_workers=workers,
                callback=_cb,
                cancel_check=_cancel,
            )
            self._msg_queue.put(("complete", res))
        except Exception as e:
            self._msg_queue.put(("error", str(e)))

    def _update_progress(self, status: str, prog: float):
        try:
            self.status_label.configure(text=status)
            if prog >= 0.0:
                self.progress_bar.set(min(1.0, max(0.0, prog)))
                pct = int(min(1.0, max(0.0, prog)) * 100)
                self.percent_label.configure(text=f"{pct}%")
            if "Completed:" in status or "Skipped:" in status:
                self._append_log(status)
        except Exception:
            pass

    def _on_downsize_complete(self, res: dict):
        self.is_processing = False
        self.start_btn.configure(
            state="normal",
            text="Start Downsizing",
            fg_color="#1DB954",
            hover_color="#1AA34A",
            text_color="#000000",
        )

        if res.get("cancelled"):
            self.status_label.configure(text="Downsizing cancelled.")
            self._append_log("Operation was cancelled.")
            return

        processed = res.get("processed", 0)
        skipped = res.get("skipped", 0)
        saved_mb = res.get("saved_bytes", 0) / (1024 * 1024)
        orig_mb = res.get("original_bytes", 0) / (1024 * 1024)
        new_mb = res.get("new_bytes", 0) / (1024 * 1024)

        if orig_mb > 1024:
            size_summary = f"{orig_mb/1024:.2f} GB -> {new_mb/1024:.2f} GB"
        else:
            size_summary = f"{orig_mb:.1f} MB -> {new_mb:.1f} MB"

        if saved_mb > 1024:
            saved_str = f"{saved_mb/1024:.2f} GB"
        else:
            saved_str = f"{saved_mb:.1f} MB"

        msg = f"Done! Downsized {processed} files"
        if skipped > 0:
            msg += f", skipped {skipped} (already <= target quality)"
        msg += f".\nSize: {size_summary}\nStorage Saved: {saved_str}!"

        self.status_label.configure(text=f"Done! Storage saved: {saved_str}")
        self.progress_bar.set(1.0)
        self.percent_label.configure(text="100%")
        self._append_log("─" * 45)
        self._append_log(msg)
        self._scan_folder_stats()

        messagebox.showinfo("Downsizing Complete", msg)

    def _on_downsize_error(self, err: str):
        self.is_processing = False
        self.start_btn.configure(
            state="normal",
            text="Start Downsizing",
            fg_color="#1DB954",
            hover_color="#1AA34A",
            text_color="#000000",
        )
        self.status_label.configure(text=f"Error: {err}")
        self._append_log(f"ERROR: {err}")
        messagebox.showerror("Downsizing Error", f"An error occurred:\n\n{err}")

    def _on_close(self):
        if self.is_processing:
            if messagebox.askyesno("Confirm Stop", "Downsizing is in progress. Stop and close?"):
                self._cancel_requested = True
                self._closed = True
                self.destroy()
        else:
            self._closed = True
            self.destroy()


class FixAlbumTagsDialog(ctk.CTkToplevel):
    """
    Dialog to repair and unify album tags for Apple Music / iOS / iTunes.
    Solves the 'split album' problem by ensuring Album Artist (TPE2 / aART)
    is consistent across all tracks while preserving contributing artists in TPE1.
    100% lossless: no audio transcoding.
    """
    def __init__(self, parent: ctk.CTk, default_folder: Path):
        super().__init__(parent)

        self.title("Fix Split Albums (Apple Music / iTunes)")
        self.geometry("640x520")
        self.minsize(580, 460)
        self.transient(parent)

        icon_path = SCRIPT_DIR / "icon.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.is_processing = False
        self._closed = False
        self._msg_queue = queue.Queue()
        self.selected_folder = Path(default_folder).expanduser().resolve()

        self._create_widgets()
        self._poll_queue()

    def _poll_queue(self):
        if self._closed:
            return
        try:
            while not self._msg_queue.empty():
                m_type, payload = self._msg_queue.get_nowait()
                if m_type == "status":
                    status, prog = payload
                    self.status_label.configure(text=status)
                    if prog >= 0:
                        self.progress_bar.set(prog)
                        self.pct_label.configure(text=f"{int(prog * 100)}%")
                elif m_type == "log":
                    self._append_log(payload)
                elif m_type == "complete":
                    res = payload
                    self.is_processing = False
                    self.start_btn.configure(state="normal", text="Fix Albums Again")
                    self.progress_bar.set(1.0)
                    self.pct_label.configure(text="100%")
                    self.status_label.configure(
                        text=f"Complete! Fixed {res.get('fixed_files', 0)} songs across {res.get('albums_fixed', 0)} albums.",
                        text_color="#81C784"
                    )
                    self._append_log("\nAlbum consolidation finished!")
                    self._append_log(f"Total files checked: {res.get('total_files', 0)}")
                    self._append_log(f"Files updated: {res.get('fixed_files', 0)}")
                    self._append_log(f"Albums consolidated: {res.get('albums_fixed', 0)}")
                    for item in res.get("fixed_albums_list", []):
                        self._append_log(f"  - {item}")
                elif m_type == "error":
                    self.is_processing = False
                    self.start_btn.configure(state="normal", text="Scan & Fix Album Tags")
                    self.status_label.configure(text=f"Error: {payload}", text_color="#E53935")
                    self._append_log(f"Error: {payload}")
        except Exception:
            pass
        if not self._closed:
            self.after(50, self._poll_queue)

    def _create_widgets(self):
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=16)

        # Header
        title = ctk.CTkLabel(
            container,
            text="Fix Split Albums for Apple Music & iOS",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#CE93D8",
            anchor="w",
        )
        title.pack(fill="x", pady=(0, 2))

        subtitle = ctk.CTkLabel(
            container,
            text="Unifies Album Artist tags so albums with featured singers (e.g. Gorillaz feat. Snoop Dogg)\nstay grouped into a single album in Apple Music and iTunes. 100% lossless (no audio re-encoding).",
            font=ctk.CTkFont(size=11),
            text_color="gray70",
            justify="left",
            anchor="w",
        )
        subtitle.pack(fill="x", pady=(0, 12))

        # Folder card
        folder_card = ctk.CTkFrame(container, fg_color="#1E1E1E", corner_radius=10)
        folder_card.pack(fill="x", pady=(0, 12))
        f_inner = ctk.CTkFrame(folder_card, fg_color="transparent")
        f_inner.pack(fill="x", padx=14, pady=12)

        ctk.CTkLabel(f_inner, text="Music Folder to Scan:", font=ctk.CTkFont(size=12, weight="bold"), text_color="gray85").pack(anchor="w", pady=(0, 4))
        row = ctk.CTkFrame(f_inner, fg_color="transparent")
        row.pack(fill="x")
        self.folder_entry = ctk.CTkEntry(row, height=32, font=ctk.CTkFont(size=12))
        self.folder_entry.insert(0, str(self.selected_folder))
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        browse_btn = ctk.CTkButton(row, text="Browse...", width=80, height=32, command=self._browse_folder)
        browse_btn.pack(side="left")

        # Action Buttons
        btn_row = ctk.CTkFrame(container, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 10))

        self.start_btn = ctk.CTkButton(
            btn_row,
            text="Scan & Fix Album Tags",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=36,
            fg_color="#7B1FA2",
            hover_color="#9C27B0",
            command=self._start_fix,
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        close_btn = ctk.CTkButton(
            btn_row,
            text="Close",
            width=90,
            height=36,
            fg_color="#333333",
            hover_color="#444444",
            command=self._close,
        )
        close_btn.pack(side="left")

        # Progress row
        p_row = ctk.CTkFrame(container, fg_color="transparent")
        p_row.pack(fill="x", pady=(0, 4))
        self.status_label = ctk.CTkLabel(p_row, text="Ready to scan and fix tags.", font=ctk.CTkFont(size=11), text_color="gray80", anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.pct_label = ctk.CTkLabel(p_row, text="0%", font=ctk.CTkFont(size=11), text_color="gray60")
        self.pct_label.pack(side="right")

        self.progress_bar = ctk.CTkProgressBar(container, height=6)
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", pady=(0, 10))

        # Log box
        self.log_box = ctk.CTkTextbox(container, height=160, font=ctk.CTkFont(family="Consolas", size=10), fg_color="#141414")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.insert("end", "Click 'Scan & Fix Album Tags' to consolidate split albums.\nYour music audio files will remain 100% byte-for-byte identical; only the ID3/Apple grouping tags are updated.\n")
        self.log_box.configure(state="disabled")

    def _browse_folder(self):
        f = ctk.filedialog.askdirectory(title="Select Music Folder", initialdir=str(self.selected_folder))
        if f:
            self.selected_folder = Path(f).resolve()
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, str(self.selected_folder))

    def _append_log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _start_fix(self):
        if self.is_processing:
            return
        f_path = Path(self.folder_entry.get().strip()).expanduser().resolve()
        if not f_path.exists():
            self.status_label.configure(text="Folder does not exist", text_color="#E53935")
            return

        self.is_processing = True
        self.start_btn.configure(state="disabled", text="Fixing Tags...")
        self.status_label.configure(text="Scanning and fixing album tags...", text_color="#CE93D8")
        self.progress_bar.set(0.1)

        def _worker():
            try:
                def cb(msg, prog):
                    self._msg_queue.put(("status", (msg, prog)))
                    self._msg_queue.put(("log", msg))

                res = fix_library_album_tags(f_path, callback=cb)
                self._msg_queue.put(("complete", res))
            except Exception as exc:
                self._msg_queue.put(("error", str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def _close(self):
        self._closed = True
        self.destroy()


def launch():
    app = SpotifyDownloaderApp()
    app.mainloop()


if __name__ == "__main__":
    launch()
