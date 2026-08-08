import os
import sys
import io
import math
import json
import shutil
import zipfile
import tempfile
import threading
import urllib.request
import urllib.error
import subprocess
import time
import winreg
import webbrowser
import tkinter as tk
import customtkinter as ctk
from concurrent.futures import ThreadPoolExecutor, as_completed
import encodings
# =====================================================================
# PRODUCTION CONFIGURATION (Update these with your hosting URLs today)
# =====================================================================
REMOTE_VERSION_URL = "https://raw.githubusercontent.com/ValCC0/XPY-LAUNCHER/refs/heads/main/version.txt"
REMOTE_UPDATE_ZIP_URL = "https://github.com/ValCC0/XPY-LAUNCHER/raw/refs/heads/main/launcher-update.zip"

# Launcher self-update version (this app's own version, kept in version.txt)
LOCAL_VERSION_FILE = "version.txt"
DEFAULT_VERSION = "1.0.0"

# Cached Roblox build version (separate so launcher-updater and Roblox cache don't collide)
ROBLOX_CACHE_FILE = "roblox_version.txt"
DEFAULT_ROBLOX_VERSION = "version-145f189a6a947303"

# Live Official Installer URLs & Fallback Download Pages
ROBLOX_BOOTSTRAPPER_URL = "https://setup.rbxcdn.com/RobloxPlayerLauncher.exe"

# =====================================================================
# RDD (Roblox Deployment Downloader) — single source of truth
# =====================================================================
# RDD's version metadata comes from the Roblox clientsettings v2 endpoint.
RDD_VERSION_URL = "https://clientsettings.roblox.com/v2/client-version/WindowsPlayer"
# RDD's deployment packages live on the AWS S3 CDN (mirrors rdd.js / fetch_version.py).
RDD_CDN_HOST = "https://setup-aws.rbxcdn.com"

# Where each package's files land inside the final zip (mirrors extractRoots.player in rdd.js).
EXTRACT_ROOTS_PLAYER = {
    "RobloxApp.zip": "",
    "redist.zip": "",
    "shaders.zip": "shaders/",
    "ssl.zip": "ssl/",
    "WebView2.zip": "",
    "WebView2RuntimeInstaller.zip": "WebView2RuntimeInstaller/",
    "content-avatar.zip": "content/avatar/",
    "content-configs.zip": "content/configs/",
    "content-fonts.zip": "content/fonts/",
    "content-sky.zip": "content/sky/",
    "content-sounds.zip": "content/sounds/",
    "content-textures2.zip": "content/textures/",
    "content-models.zip": "content/models/",
    "content-platform-fonts.zip": "PlatformContent/pc/fonts/",
    "content-platform-dictionaries.zip": "PlatformContent/pc/shared_compression_dictionaries/",
    "content-terrain.zip": "PlatformContent/pc/terrain/",
    "content-textures3.zip": "PlatformContent/pc/textures/",
    "extracontent-luapackages.zip": "ExtraContent/LuaPackages/",
    "extracontent-translations.zip": "ExtraContent/translations/",
    "extracontent-models.zip": "ExtraContent/models/",
    "extracontent-textures.zip": "ExtraContent/textures/",
    "extracontent-places.zip": "ExtraContent/places/",
}

APP_SETTINGS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Settings>
    <ContentFolder>content</ContentFolder>
    <BaseUrl>http://www.roblox.com</BaseUrl>
</Settings>
"""

# --- Animation / color utilities ---
def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _rgb_to_hex(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"

def _lerp_color(c1, c2, t):
    """Linearly interpolate between two hex colors; t in [0, 1]."""
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return _rgb_to_hex(r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t)

# Animation timing
_ANIM_MS = 30          # frame interval (~33 fps)
_HOVER_FRAMES = 6      # ~180ms for hover transitions
_TAB_FADE_FRAMES = 5    # ~150ms for tab crossfade
_PULSE_FRAMES = 60     # ~2s full pulse cycle

EXTERNAL_APPS = [
    {
        "name": "Madium Bootstrapper",
        "desc": "Mad.exe (Current Build)",
        "check_paths": [
            os.path.join(os.getcwd(), "Madium-Bootstrapper.exe"),
            os.path.join(os.getcwd(), "Mad.exe"),
            os.path.expandvars(r"%LocalAppData%\Mad\Mad.exe"),
        ],
        "download_url": "https://github.com/ValCC0/XPY-LAUNCHER/raw/refs/heads/main/Madium-Bootstrapper.exe",
        "save_as": "Madium-Bootstrapper.exe",
        "fallback_download_page": "https://github.com/ValCC0/XPY-LAUNCHER"
    },
    {
        "name": "Real Update.exe",
        "desc": "Mad.exe (Real / Update)",
        "check_paths": [
            os.path.join(os.getcwd(), "Update.exe"),
            os.path.join(os.getcwd(), "Real.exe"),
            os.path.expandvars(r"%LocalAppData%\Real\Update.exe"),
        ],
        "download_url": "https://raw.githubusercontent.com/ValCC0/XPY-LAUNCHER/refs/heads/main/Update.exe",
        "save_as": "Update.exe",
        "fallback_download_page": "https://github.com/ValCC0/XPY-LAUNCHER"
    }
]

# Color Palette (Matching Phobia Loader)
BG_COLOR = "#0c0b0e"
SIDEBAR_BG = "#080709"
CARD_BG = "#121116"
CARD_BORDER_RED = "#3d1e28"
CARD_BORDER_GREEN = "#1a3325"
ACCENT_PINK = "#5fa9ff"  # Light blue accent
ACCENT_GREEN = "#1f8749"
TEXT_WHITE = "#ffffff"
TEXT_GRAY = "#8f8e94"
TEXT_MUTED = "#55545a"
PILL_BG = "#1e1c22"
PILL_BG_GREEN = "#152e20"

class XPYLauncherApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Launcher's own version (kept in version.txt) — used for launcher self-update checks only.
        self.launcher_version = self.load_launcher_version()
        # Roblox local build version — populated by detect_roblox_path()/check_roblox_status().
        self.local_version = "Not Installed"
        self.remote_version = "Checking..."
        self.remote_source = None
        self.is_roblox_installed = False
        self.roblox_path = ""
        
        # UI page state
        self.active_tab = 0 # 0: Roblox, 1: App Fetcher

        # Window Settings (Frameless for sleek styling)
        self.title("XPY LAUNCHER")
        self.geometry("840x620")
        self._normal_geometry = self.geometry()
        self.configure(fg_color=BG_COLOR)
        self.overrideredirect(True)

        # High DPI support
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        # Make window draggable
        self.bind("<Button-1>", self.start_move)
        self.bind("<B1-Motion>", self.on_move)

        self.setup_ui()
        # Initialize status and log console
        self.log_console = ctk.CTkTextbox(self.main_container, width=400, height=200, wrap='none')
        self.log_console.pack(side='bottom', fill='x')
        self.log_console.pack_forget()  # hidden by default
        self.status_indicator = ctk.CTkLabel(self.top_bar, text="Ready", font=("Segoe UI", 12), text_color=TEXT_GRAY)
        self.status_indicator.pack(side='right', padx=5)
        # Detect installed Roblox path
        self.detect_roblox_path()
        self.check_roblox_status()
        self.switch_tab(0)
        # Wire up animations
        self._apply_animations()
        self.check_updates_async()

    def load_launcher_version(self):
        """Load this launcher's own version from version.txt (used for launcher self-update checks)."""
        if os.path.exists(LOCAL_VERSION_FILE):
            try:
                with open(LOCAL_VERSION_FILE, "r") as f:
                    ver = f.read().strip()
                    if ver:
                        return ver
            except Exception:
                pass
        return DEFAULT_VERSION

    def save_launcher_version(self, version):
        """Persist the launcher's own version after a successful self-update."""
        try:
            with open(LOCAL_VERSION_FILE, "w") as f:
                f.write(version)
            self.launcher_version = version
        except Exception:
            pass

    def load_cached_roblox_version(self):
        """Load the cached Roblox build version (roblox_version.txt). Returns None if not cached."""
        if os.path.exists(ROBLOX_CACHE_FILE):
            try:
                with open(ROBLOX_CACHE_FILE, "r") as f:
                    ver = f.read().strip()
                    if ver:
                        return ver
            except Exception:
                pass
        return None

    def save_cached_roblox_version(self, version):
        try:
            with open(ROBLOX_CACHE_FILE, "w") as f:
                f.write(version)
        except Exception:
            pass



    def start_move(self, event):
        self._x = event.x
        self._y = event.y

    def on_move(self, event):
        x = self.winfo_x() + event.x - self._x
        y = self.winfo_y() + event.y - self._y
        self.geometry(f"+{x}+{y}")

    def setup_ui(self):
        # 1. Sidebar Frame
        self.sidebar = ctk.CTkFrame(self, width=65, fg_color=SIDEBAR_BG, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        # Heartbeat canvas button (Roblox Tab)
        self.heart_btn = ctk.CTkButton(self.sidebar, text="", width=48, height=48, corner_radius=12,
                                       fg_color="transparent", hover_color="#1a1218",
                                       command=lambda: self.switch_tab(0))
        self.heart_btn.pack(pady=(20, 20))
        
        self.heart_canvas = tk.Canvas(self.heart_btn, width=32, height=32, bg=SIDEBAR_BG, highlightthickness=0)
        self.heart_canvas.place(relx=0.5, rely=0.5, anchor="center")
        self.heart_canvas.bind("<Button-1>", lambda e: self.switch_tab(0))
        self.draw_heartbeat_icon()

        # Leaf button (Apps Tab)
        self.leaf_btn = ctk.CTkButton(self.sidebar, text="🍃", font=("Segoe UI", 20), width=48, height=48,
                                      corner_radius=12, fg_color="transparent", hover_color="#121814",
                                      text_color=TEXT_GRAY, command=lambda: self.switch_tab(1))
        self.leaf_btn.pack(pady=10)

        # Refresh / Sync button
        self.sync_btn = ctk.CTkButton(self.sidebar, text="🔄", font=("Segoe UI", 18), width=48, height=48,
                                      corner_radius=12, fg_color="transparent", hover_color="#18181c",
                                      text_color=TEXT_GRAY, command=self.check_updates_async)
        self.sync_btn.pack(pady=10)

        # Language pill
        en_lbl = ctk.CTkLabel(self.sidebar, text="EN", font=("Segoe UI Semibold", 11), 
                               fg_color=PILL_BG, width=28, height=28, corner_radius=14)
        en_lbl.pack(side="bottom", pady=25)

        # 2. Main Area Frame
        self.main_container = ctk.CTkFrame(self, fg_color=BG_COLOR, corner_radius=0)
        self.main_container.pack(side="right", fill="both", expand=True)

        # 3. Top Title/Header Bar
        self.top_bar = ctk.CTkFrame(self.main_container, height=60, fg_color="transparent")
        self.top_bar.pack(fill="x", padx=30, pady=(15, 0))

        self.title_lbl = ctk.CTkLabel(self.top_bar, text="XPY Launcher", font=("Segoe UI Semibold", 16), text_color=TEXT_WHITE)
        self.title_lbl.pack(side="left")

        # Window Action Controls
        close_btn = ctk.CTkButton(self.top_bar, text="✕", width=28, height=28, corner_radius=14, 
                                  fg_color="#18171c", hover_color="#c42b2b", text_color=TEXT_GRAY, font=("Segoe UI", 12),
                                  command=self.destroy)
        close_btn.pack(side="right", padx=(5, 0))

        min_btn = ctk.CTkButton(self.top_bar, text="—", width=28, height=28, corner_radius=14, 
                                fg_color="#18171c", hover_color="#2b2b30", text_color=TEXT_GRAY, font=("Segoe UI", 10),
                                command=self.withdraw)
        min_btn.pack(side="right", padx=5)

        # Fullscreen toggle button
        fullscreen_btn = ctk.CTkButton(self.top_bar, text="🗖", width=28, height=28, corner_radius=14,
                                       fg_color="#18171c", hover_color="#2b2b30", text_color=TEXT_GRAY, font=("Segoe UI", 12),
                                       command=self.toggle_fullscreen)
        fullscreen_btn.pack(side="right", padx=5)

        profile_lbl = ctk.CTkLabel(self.top_bar, text="👤", font=("Segoe UI", 14), 
                                   fg_color=PILL_BG, width=28, height=28, corner_radius=14, text_color=ACCENT_PINK)
        profile_lbl.pack(side="right", padx=15)

        local_lbl = ctk.CTkLabel(self.top_bar, text="Local", font=("Segoe UI", 12), text_color=TEXT_GRAY)
        local_lbl.pack(side="right")
        # Add Clean Start button
        clean_btn = ctk.CTkButton(self.top_bar, text="🧹", width=28, height=28, corner_radius=14,
                                    fg_color="#18171c", hover_color="#2b2b30", text_color=TEXT_GRAY, font=("Segoe UI", 12),
                                    command=self.clean_start)
        clean_btn.pack(side="right", padx=5)

        # 4. View Container
        self.view_container = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.view_container.pack(fill="both", expand=True, padx=30, pady=(10, 0))

        # --- Sub-view: Roblox Tab ---
        self.roblox_view = ctk.CTkFrame(self.view_container, fg_color="transparent")
        
        # Stay up to date header
        title_box = ctk.CTkFrame(self.roblox_view, fg_color="transparent")
        title_box.pack(anchor="w", pady=(15, 5))
        stay_lbl = ctk.CTkLabel(title_box, text="Stay ", font=("Segoe UI Black", 32), text_color=TEXT_WHITE)
        stay_lbl.pack(side="left")
        up_lbl = ctk.CTkLabel(title_box, text="up to date.", font=("Segoe UI Black", 32), text_color=ACCENT_PINK)
        up_lbl.pack(side="left")

        desc_lbl = ctk.CTkLabel(self.roblox_view, text="Download the newest roblox version.", 
                                font=("Segoe UI", 14), text_color=TEXT_GRAY)
        desc_lbl.pack(anchor="w", pady=(0, 20))

        # Divider line
        status_header = ctk.CTkFrame(self.roblox_view, fg_color="transparent")
        status_header.pack(fill="x", pady=(10, 20))
        sh_lbl = ctk.CTkLabel(status_header, text="VERSION STATUS", font=("Segoe UI Bold", 11), text_color=TEXT_MUTED)
        sh_lbl.pack(side="left")
        sh_line = ctk.CTkFrame(status_header, height=1, fg_color="#1d1b22")
        sh_line.pack(side="left", fill="x", expand=True, padx=(10, 0))

        # Cards frame
        cards_box = ctk.CTkFrame(self.roblox_view, fg_color="transparent")
        cards_box.pack(fill="both", expand=True)

        # Roblox local status card
        self.left_card = ctk.CTkFrame(cards_box, fg_color=CARD_BG, border_width=1.5, 
                                       border_color=CARD_BORDER_RED, width=360, height=220)
        self.left_card.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=5)
        self.left_card.pack_propagate(False)

        left_pill_box = ctk.CTkFrame(self.left_card, fg_color="transparent")
        left_pill_box.pack(fill="x", padx=15, pady=15)
        self.installed_pill = ctk.CTkLabel(left_pill_box, text="💾 Installed", font=("Segoe UI Semibold", 10),
                                            fg_color=PILL_BG, text_color=TEXT_GRAY,
                                            width=80, height=22, corner_radius=11)
        self.installed_pill.pack(side="left")

        left_info_box = ctk.CTkFrame(self.left_card, fg_color="transparent")
        left_info_box.pack(fill="x", padx=15, pady=(5, 10))
        folder_icon_lbl = ctk.CTkLabel(left_info_box, text="📂", font=("Segoe UI", 24), 
                                       fg_color="#20151d", text_color=ACCENT_PINK, 
                                       width=48, height=48, corner_radius=12)
        folder_icon_lbl.pack(side="left", padx=(0, 15))

        left_text_box = ctk.CTkFrame(left_info_box, fg_color="transparent")
        left_text_box.pack(side="left", fill="y")
        l_title = ctk.CTkLabel(left_text_box, text="Installed", font=("Segoe UI Bold", 15), text_color=TEXT_WHITE)
        l_title.pack(anchor="w")
        self.local_ver_label = ctk.CTkLabel(left_text_box, text=self.local_version, font=("Segoe UI", 12), text_color=TEXT_GRAY)
        self.local_ver_label.pack(anchor="w")

        self.local_status_lbl = ctk.CTkLabel(self.left_card, text="Checking Roblox installation...", font=("Segoe UI", 12), text_color=TEXT_MUTED)
        self.local_status_lbl.pack(anchor="w", padx=15, pady=(0, 15))

        self.launch_btn = ctk.CTkButton(self.left_card, text="⚡ LAUNCH ROBLOX", font=("Segoe UI Black", 13),
                                        fg_color=ACCENT_PINK, hover_color="#3b82f6", text_color="#ffffff",
                                        border_width=1, border_color="#60a5fa",
                                        height=42, corner_radius=12, command=self.launch_roblox)
        self.launch_btn.pack(fill="x", padx=15, side="bottom", pady=15)

        self.install_roblox_btn = ctk.CTkButton(self.left_card, text="📥 INSTALL ROBLOX", font=("Segoe UI Black", 13),
                                                fg_color="#3b82f6", hover_color="#2563eb", text_color="#ffffff",
                                                border_width=1, border_color="#93c5fd",
                                                height=42, corner_radius=12, command=self.start_roblox_download)
        # Shown only when Roblox is not installed (packed dynamically)

        # Roblox remote build card
        self.right_card = ctk.CTkFrame(cards_box, fg_color=CARD_BG, border_width=1.5,
                                        border_color=CARD_BORDER_GREEN, width=360, height=220)
        self.right_card.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=5)
        self.right_card.pack_propagate(False)

        right_pill_box = ctk.CTkFrame(self.right_card, fg_color="transparent")
        right_pill_box.pack(fill="x", padx=15, pady=15)
        self.up_to_date_pill = ctk.CTkLabel(right_pill_box, text="✓ Up to Date", font=("Segoe UI Semibold", 10),
                                            fg_color=PILL_BG_GREEN, text_color=ACCENT_GREEN,
                                            width=85, height=22, corner_radius=11)
        self.up_to_date_pill.pack(side="left")

        right_info_box = ctk.CTkFrame(self.right_card, fg_color="transparent")
        right_info_box.pack(fill="x", padx=15, pady=(5, 10))
        globe_icon_lbl = ctk.CTkLabel(right_info_box, text="🌐", font=("Segoe UI", 24), 
                                      fg_color="#12251a", text_color=ACCENT_GREEN, 
                                      width=48, height=48, corner_radius=12)
        globe_icon_lbl.pack(side="left", padx=(0, 15))

        right_text_box = ctk.CTkFrame(right_info_box, fg_color="transparent")
        right_text_box.pack(side="left", fill="y")
        r_title = ctk.CTkLabel(right_text_box, text="Official Build", font=("Segoe UI Bold", 15), text_color=TEXT_WHITE)
        r_title.pack(anchor="w")
        self.remote_ver_label = ctk.CTkLabel(right_text_box, text=self.remote_version, font=("Segoe UI", 12), text_color=TEXT_GRAY)
        self.remote_ver_label.pack(anchor="w")

        # Remote status label
        self.remote_status_lbl = ctk.CTkLabel(self.right_card, text="Fetched", font=("Segoe UI", 12), text_color=TEXT_MUTED)
        self.remote_status_lbl.pack(anchor="w", padx=15, pady=(0, 15))

        # Download current build button — always visible on right card
        self.download_build_btn = ctk.CTkButton(self.right_card, text="📥 DOWNLOAD LATEST BUILD", font=("Segoe UI Black", 12),
                                                fg_color="#10b981", hover_color="#059669", text_color=TEXT_WHITE,
                                                border_width=1, border_color="#34d399",
                                                height=42, corner_radius=12, command=self.start_roblox_download)
        self.download_build_btn.pack(fill="x", padx=15, side="bottom", pady=(0, 15))

        self.update_action_btn = ctk.CTkButton(self.right_card, text="🔄 UPDATE LAUNCHER", font=("Segoe UI Black", 12),
                                               fg_color="#ef4444", hover_color="#dc2626", text_color=TEXT_WHITE,
                                               border_width=1, border_color="#f87171",
                                               height=36, corner_radius=10, command=self.trigger_update_async)
        self.update_action_btn.pack(fill="x", padx=15, side="bottom", pady=(0, 10))

        # --- Sub-view: Apps Tab ---
        self.apps_view = ctk.CTkFrame(self.view_container, fg_color="transparent")
        
        # Header Apps
        apps_title_box = ctk.CTkFrame(self.apps_view, fg_color="transparent")
        apps_title_box.pack(anchor="w", pady=(15, 5))
        a_stay_lbl = ctk.CTkLabel(apps_title_box, text="App ", font=("Segoe UI Black", 32), text_color=TEXT_WHITE)
        a_stay_lbl.pack(side="left")
        a_up_lbl = ctk.CTkLabel(apps_title_box, text="Fetcher.", font=("Segoe UI Black", 32), text_color=ACCENT_PINK)
        a_up_lbl.pack(side="left")

        a_desc_lbl = ctk.CTkLabel(self.apps_view, text="Verify local applications and fetch installers.", 
                                font=("Segoe UI", 14), text_color=TEXT_GRAY)
        a_desc_lbl.pack(anchor="w", pady=(0, 20))

        # Apps Divider
        a_status_header = ctk.CTkFrame(self.apps_view, fg_color="transparent")
        a_status_header.pack(fill="x", pady=(10, 20))
        ash_lbl = ctk.CTkLabel(a_status_header, text="EXTERNAL UTILITIES", font=("Segoe UI Bold", 11), text_color=TEXT_MUTED)
        ash_lbl.pack(side="left")
        ash_line = ctk.CTkFrame(a_status_header, height=1, fg_color="#1d1b22")
        ash_line.pack(side="left", fill="x", expand=True, padx=(10, 0))

        # Container for Apps Cards (scrollable for future growth)
        self.app_scroll = ctk.CTkScrollableFrame(self.apps_view, fg_color="transparent", scrollbar_fg_color="#2a2830")
        self.app_scroll.pack(fill="both", expand=True, padx=(0, 5))
        self.app_list_container = self.app_scroll

        self.app_widgets = []
        for app in EXTERNAL_APPS:
            card = ctk.CTkFrame(self.app_list_container, fg_color=CARD_BG, corner_radius=12,
                                border_width=1, border_color="#1d1b22")
            card.pack(fill="x", pady=6, padx=(0, 10))

            # --- Top row: icon + name/desc/note ---
            top_row = ctk.CTkFrame(card, fg_color="transparent")
            top_row.pack(fill="x", padx=16, pady=(14, 4))

            icon_lbl = ctk.CTkLabel(top_row, text="📦", font=("Segoe UI", 22),
                                    fg_color="#20151d", text_color=ACCENT_PINK,
                                    width=44, height=44, corner_radius=10)
            icon_lbl.pack(side="left", padx=(0, 12))

            name_box = ctk.CTkFrame(top_row, fg_color="transparent")
            name_box.pack(side="left", fill="x", expand=True)
            lbl_name = ctk.CTkLabel(name_box, text=app["name"], font=("Segoe UI Bold", 14), text_color=TEXT_WHITE)
            lbl_name.pack(anchor="w")
            lbl_desc = ctk.CTkLabel(name_box, text=app["desc"], font=("Segoe UI", 11), text_color=TEXT_MUTED)
            lbl_desc.pack(anchor="w")
            lbl_note = ctk.CTkLabel(name_box, text="💡 If launch failed then download CloudFlare One Client, it will work.",
                                    font=("Segoe UI Italic", 10), text_color="#60a5fa", wraplength=400, justify="left")
            lbl_note.pack(anchor="w", pady=(3, 0))

            # --- Bottom row: status + action button ---
            bottom_row = ctk.CTkFrame(card, fg_color="transparent")
            bottom_row.pack(fill="x", padx=16, pady=(4, 14))

            lbl_status = ctk.CTkLabel(bottom_row, text="Checking...", font=("Segoe UI", 11), text_color=TEXT_GRAY)
            lbl_status.pack(side="left")

            btn_action = ctk.CTkButton(bottom_row, text="Check", font=("Segoe UI Bold", 12),
                                       fg_color=PILL_BG, hover_color="#2b2b30", text_color=TEXT_WHITE,
                                       width=110, height=34, corner_radius=8)
            btn_action.pack(side="right")

            # Store references for status updates
            self.app_widgets.append({
                "app": app,
                "card": card,
                "lbl_status": lbl_status,
                "btn_action": btn_action
            })

        self.update_apps_status()

        # 5. Footer Status Bar
        self.footer = ctk.CTkFrame(self.main_container, height=45, fg_color="transparent")
        self.footer.pack(fill="x", side="bottom", padx=30, pady=10)

        self.status_dot = ctk.CTkLabel(self.footer, text="●", text_color=ACCENT_GREEN, font=("Segoe UI", 14))
        self.status_dot.pack(side="left", padx=(0, 5))

        self.status_text = ctk.CTkLabel(self.footer, text="Updated", font=("Segoe UI", 12), text_color=TEXT_GRAY)
        self.status_text.pack(side="left")

        # Progress bar
        self.progress_bar = ctk.CTkProgressBar(self.footer, width=200, height=8, fg_color="#18171c", progress_color=ACCENT_PINK)
        self.progress_bar.set(0)

    def draw_heartbeat_icon(self):
        # Clears and draws pink pulse
        self.heart_canvas.delete("all")
        self.heart_canvas.configure(bg=SIDEBAR_BG)
        # Subtle glowing background border on canvas
        self.heart_canvas.create_rectangle(1, 1, 31, 31, fill="#1c0f16", outline="#291120", width=1)
        self.heart_canvas.create_line(4, 16, 10, 16, fill=ACCENT_PINK, width=2)
        self.heart_canvas.create_line(10, 16, 12, 8, fill=ACCENT_PINK, width=2)
        self.heart_canvas.create_line(12, 8, 15, 24, fill=ACCENT_PINK, width=2)
        self.heart_canvas.create_line(15, 24, 18, 12, fill=ACCENT_PINK, width=2)
        self.heart_canvas.create_line(18, 12, 20, 16, fill=ACCENT_PINK, width=2)
        self.heart_canvas.create_line(20, 16, 28, 16, fill=ACCENT_PINK, width=2)

    # --- Animation System ---
    def _animate_color(self, widget, attr, target_color, frames=_HOVER_FRAMES, callback=None):
        """Smoothly transition a widget's color attribute (e.g. 'border_color', 'fg_color')."""
        try:
            current = widget.cget(attr)
        except Exception:
            if callback:
                callback()
            return
        if current == target_color:
            if callback:
                callback()
            return
        def step(i):
            if i > frames:
                try:
                    widget.configure(**{attr: target_color})
                except Exception:
                    pass
                if callback:
                    callback()
                return
            t = i / frames
            widget.configure(**{attr: _lerp_color(current, target_color, t)})
            self.after(_ANIM_MS, lambda: step(i + 1))
        step(1)

    def _bind_hover_glow(self, card, enter_color, leave_color):
        """Bind hover enter/leave to animate card border_color."""
        card.bind("<Enter>", lambda e: self._animate_color(card, "border_color", enter_color))
        card.bind("<Leave>", lambda e: self._animate_color(card, "border_color", leave_color))

    def _bind_button_press(self, btn, press_color, release_color):
        """Darken button on press, restore on release."""
        def on_press(e):
            try:
                btn.configure(fg_color=press_color)
            except Exception:
                pass
        def on_release(e):
            try:
                btn.configure(fg_color=release_color)
            except Exception:
                pass
        btn.bind("<ButtonPress-1>", on_press)
        btn.bind("<ButtonRelease-1>", on_release)

    def _pulse_status_dot(self):
        """Gently pulse the footer status dot between bright and dim green."""
        if not self.winfo_exists():
            return
        bright = ACCENT_GREEN
        dim = "#145a2e"
        frame = getattr(self, "_pulse_frame", 0)
        self._pulse_frame = frame + 1
        t = (math.sin(2 * math.pi * frame / _PULSE_FRAMES) + 1) / 2
        try:
            self.status_dot.configure(text_color=_lerp_color(dim, bright, t))
        except Exception:
            return
        self.after(_ANIM_MS, self._pulse_status_dot)

    def _pulse_background(self):
        """Subtly shift the background color to create a living atmosphere."""
        if not self.winfo_exists():
            return
        color_a = BG_COLOR
        color_b = "#12111a"
        frame = getattr(self, "_bg_pulse_frame", 0)
        self._bg_pulse_frame = frame + 1
        t = (math.sin(2 * math.pi * frame / 120) + 1) / 2
        try:
            self.configure(fg_color=_lerp_color(color_a, color_b, t))
        except Exception:
            return
        self.after(_ANIM_MS, self._pulse_background)

    def _smooth_progress_lerp(self, target):
        """Smoothly animate the progress bar toward target value."""
        current = getattr(self, "_smooth_prog_current", 0.0)
        if abs(current - target) < 0.005:
            self.progress_bar.set(target)
            self._smooth_prog_current = target
            return
        step_val = 0.04 if target > current else -0.04
        new_val = max(0.0, min(1.0, current + step_val))
        self.progress_bar.set(new_val)
        self._smooth_prog_current = new_val
        self.after(_ANIM_MS, lambda: self._smooth_progress_lerp(target))

    def _apply_animations(self):
        """Wire up all animations after UI is built."""
        # Card hover glow — Roblox tab cards
        self._bind_hover_glow(self.left_card, "#5a2040", CARD_BORDER_RED)
        self._bind_hover_glow(self.right_card, "#1a4030", CARD_BORDER_GREEN)

        # Card hover glow — App Fetcher cards
        for item in self.app_widgets:
            self._bind_hover_glow(item["card"], "#3a2850", "#1d1b22")

        # Button press feedback — all major buttons
        for btn, press, release in [
            (self.launch_btn, "#2563eb", ACCENT_PINK),
            (self.install_roblox_btn, "#1d4ed8", "#3b82f6"),
            (self.download_build_btn, "#047857", "#10b981"),
            (self.update_action_btn, "#b91c1c", "#ef4444"),
        ]:
            if btn.winfo_exists():
                self._bind_button_press(btn, press, release)

        # Start status dot pulse and background pulse
        self._pulse_frame = 0
        self.after(500, self._pulse_status_dot)
        self._bg_pulse_frame = 0
        self.after(500, self._pulse_background)

    def switch_tab(self, tab_idx):
        self.active_tab = tab_idx
        if tab_idx == 0:
            # Roblox tab
            self.apps_view.pack_forget()
            self.roblox_view.pack(fill="both", expand=True)
            self.heart_btn.configure(fg_color="#1c0f16")
            self.leaf_btn.configure(fg_color="transparent")
        else:
            # Apps tab
            self.roblox_view.pack_forget()
            self.apps_view.pack(fill="both", expand=True)
            self.heart_btn.configure(fg_color="transparent")
            self.leaf_btn.configure(fg_color="#121814")

    # --- Roblox Logic ---
    def check_roblox_status(self):
        local_appdata = os.environ.get("LOCALAPPDATA")
        self.local_version = "Not Installed"
        self.is_roblox_installed = False
        
        if local_appdata:
            roblox_versions_dir = os.path.join(local_appdata, "Roblox", "Versions")
            if os.path.exists(roblox_versions_dir):
                for root_dir, _, files in os.walk(roblox_versions_dir):
                    if "RobloxPlayerBeta.exe" in files:
                        self.roblox_path = os.path.join(root_dir, "RobloxPlayerBeta.exe")
                        self.is_roblox_installed = True
                        # The directory name (e.g. version-145f189a6a947303) is the build version
                        self.local_version = os.path.basename(root_dir)
                        self.local_status_lbl.configure(text="Roblox found", text_color=TEXT_GRAY)
                        break
        
        self.local_ver_label.configure(text=self.local_version)
        self.update_action_state()

    def update_action_state(self):
        # Left card: show launch or install depending on whether Roblox is found
        if not self.is_roblox_installed:
            self.launch_btn.pack_forget()
            self.install_roblox_btn.pack(fill="x", padx=15, side="bottom", pady=15)
            self.installed_pill.configure(text="❌ Not Installed", fg_color="#30151d", text_color=ACCENT_PINK)
            self.local_status_lbl.configure(text="Roblox not found", text_color="#c42b2b")
        else:
            self.install_roblox_btn.pack_forget()
            self.launch_btn.pack(fill="x", padx=15, side="bottom", pady=15)
            if self.remote_version not in ["Checking...", "Offline / Idle"] and self.local_version != self.remote_version:
                self.installed_pill.configure(text="⚠ Outdated", fg_color="#30151d", text_color=ACCENT_PINK)
            else:
                self.installed_pill.configure(text="💾 Installed", fg_color=PILL_BG, text_color=TEXT_GRAY)

        # Right card: show the fetched remote build version on the download button
        if self.remote_version not in ["Checking...", "Offline / Idle"]:
            self.download_build_btn.configure(text=f"⇩ Download {self.remote_version}")
        else:
            self.download_build_btn.configure(text="⇩ Download Current Build")

    def launch_roblox(self):
        # Launch Roblox via URI scheme first, fallback to direct exe
        self.status_text.configure(text="Launching Roblox...")
        try:
            webbrowser.open("roblox://")
            self.status_text.configure(text="Roblox launched via URI.")
            return
        except Exception:
            pass

        if self.roblox_path:
            try:
                subprocess.Popen([self.roblox_path])
                self.status_text.configure(text="Roblox player launched.")
            except Exception as e:
                self.status_text.configure(text=f"Launch failed: {e}")

    def start_roblox_download(self):
        # Called by both Install and Download Current Build buttons
        self.status_text.configure(text="Fetching Roblox deployment (RDD)...")
        thread = threading.Thread(target=self.download_and_install_roblox, daemon=True)
        thread.start()

    # --- RDD deployment assembly (mirrors fetch_version.py) ---
    def _parse_rdd_manifest(self, manifest_text):
        """Parse rbxPkgManifest.txt (v0 format).
        Returns a list of dicts: {name, compressed, uncompressed}.
        """
        lines = [ln.strip() for ln in manifest_text.splitlines() if ln.strip()]
        if not lines or lines[0] != "v0":
            raise ValueError(f"Unexpected manifest format (first line: {lines[0]!r})")
        packages = []
        i = 1
        while i + 3 <= len(lines):
            name = lines[i]
            md5 = lines[i + 1]
            try:
                compressed = int(lines[i + 2])
                uncompressed = int(lines[i + 3])
            except ValueError:
                i += 1
                continue
            packages.append({
                "name": name, "md5": md5,
                "compressed": compressed, "uncompressed": uncompressed,
            })
            i += 4
        return packages

    def _fetch_bytes(self, url, timeout=60):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _download_rdd_deployment(self, version, dest_zip):
        """Assemble a full Roblox deployment zip from RDD (setup-aws.rbxcdn.com).
        Mirrors fetch_version.py: fetch manifest -> parallel-download zip packages
        -> extract each into its mapped root -> add AppSettings.xml -> integrity check.
        Returns True on success, False on failure.
        """
        version = version.strip()
        if not version.startswith("version-"):
            version = "version-" + version
        version_path = f"{RDD_CDN_HOST}/{version}-"

        # 1. Fetch manifest
        self.root_after_safe(lambda: self.status_text.configure(text="Fetching RDD manifest..."))
        manifest_url = version_path + "rbxPkgManifest.txt"
        manifest_text = self._fetch_bytes(manifest_url).decode("utf-8", errors="replace")
        packages = self._parse_rdd_manifest(manifest_text)
        zip_pkgs = [p for p in packages if p["name"].endswith(".zip")]
        if not zip_pkgs or "RobloxApp.zip" not in [p["name"] for p in zip_pkgs]:
            raise ValueError("Manifest missing RobloxApp.zip — not a WindowsPlayer build.")

        # 2. Parallel download of all zip packages
        self.root_after_safe(lambda: self.status_text.configure(
            text=f"Downloading {len(zip_pkgs)} RDD packages..."))
        self.root_after_safe(lambda: self._smooth_progress_lerp(0))

        downloaded = {}
        completed = 0
        total_pkgs = len(zip_pkgs)

        def fetch_one(pkg):
            url = version_path + pkg["name"]
            return pkg["name"], self._fetch_bytes(url)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(fetch_one, p): p for p in zip_pkgs}
            for fut in as_completed(futures):
                name, data = fut.result()
                downloaded[name] = data
                completed += 1
                self.root_after_safe(lambda c=completed: self._smooth_progress_lerp(c / total_pkgs))

        # 3. Assemble the final zip (extraction phase ~100%)
        self.root_after_safe(lambda: self.status_text.configure(text="Assembling deployment zip..."))
        self.root_after_safe(lambda: self._smooth_progress_lerp(1.0))
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as out_zip:
            out_zip.writestr("AppSettings.xml", APP_SETTINGS_XML)
            for name, data in downloaded.items():
                root = EXTRACT_ROOTS_PLAYER.get(name, "")
                with zipfile.ZipFile(io.BytesIO(data)) as pkg_zip:
                    for member in pkg_zip.infolist():
                        if member.is_dir():
                            continue
                        rel = member.filename.replace("\\", "/").lstrip("/")
                        out_zip.writestr(root + rel, pkg_zip.read(member))

        # 4. Integrity check
        with zipfile.ZipFile(dest_zip) as z:
            if z.testzip() is not None:
                raise ValueError("Integrity check failed on assembled zip.")
        return True

    def download_and_install_roblox(self):
        self.root_after_safe(lambda: self.progress_bar.pack(side="right", padx=15))

        version = getattr(self, "remote_version", None)
        # RDD needs a concrete build version; fall back to bootstrapper if unavailable.
        if not version or version in ["Checking...", "Offline / Idle"]:
            self._download_via_bootstrapper_fallback()
            return

        temp_dir = tempfile.mkdtemp()
        out_name = f"WEAO-LIVE-WindowsPlayer-{version}.zip"
        out_path = os.path.join(temp_dir, out_name)

        try:
            ok = self._download_rdd_deployment(version, out_path)
        except Exception as e:
            self.root_after_safe(lambda: self.status_text.configure(
                text=f"RDD download failed ({e}); retrying bootstrapper..."))
            ok = False

        if ok:
            self.root_after_safe(lambda: self.status_text.configure(
                text=f"Saved {out_name} — opening folder..."))
            try:
                # Open the output folder so the user can extract/launch manually.
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", "/select,", out_path])
                else:
                    subprocess.Popen(["open", temp_dir])
            except Exception:
                pass
            self.root_after_safe(lambda: self.progress_bar.pack_forget())
            self.root_after_safe(self.check_roblox_status)
            self.root_after_safe(lambda: self.status_indicator.configure(text="RDD download done"))
        else:
            self._download_via_bootstrapper_fallback()

    def _download_via_bootstrapper_fallback(self):
        """Last-resort fallback: official Roblox bootstrapper installer (not RDD)."""
        temp_dir = tempfile.mkdtemp()
        installer_path = os.path.join(temp_dir, "RobloxPlayerLauncher.exe")
        self.root_after_safe(lambda: self.status_text.configure(
            text="Downloading Roblox bootstrapper (fallback)..."))
        success = self.download_file_with_progress(ROBLOX_BOOTSTRAPPER_URL, installer_path)
        if success:
            self.root_after_safe(lambda: self.status_text.configure(text="Running Roblox installer..."))
            try:
                subprocess.Popen([installer_path], shell=True)
                self.root_after_safe(lambda: self.status_text.configure(text="Roblox setup launched."))
            except Exception as e:
                self.root_after_safe(lambda: self.status_text.configure(text=f"Setup failed: {e}"))
        else:
            self.root_after_safe(lambda: self.status_text.configure(text="Failed to download Roblox."))
        self.root_after_safe(lambda: self.progress_bar.pack_forget())
        self.root_after_safe(self.check_roblox_status)
        self.root_after_safe(lambda: self.status_indicator.configure(text="Ready to Launch"))

    # --- Update Checker Logic ---
    def check_updates_async(self):
        self.status_text.configure(text="Checking for updates...")
        self.status_dot.configure(text_color="#cca010") # Yellow
        thread = threading.Thread(target=self.check_updates_task, daemon=True)
        thread.start()

    def check_updates_task(self):
        # Only RDD is used as the source of truth for the remote Roblox version.
        rdd_version = self.fetch_rdd_version()
        if rdd_version:
            self.remote_version = rdd_version
            self.remote_source = "RDD"
            self.remote_download_url = f"{RDD_CDN_HOST}/{rdd_version}-"
        else:
            self.remote_version = "Offline / Idle"
            self.remote_source = None
            self.remote_download_url = None

        # Update UI safely
        self.root_after_safe(self.update_remote_ui)

    def fetch_rdd_version(self):
        """Fetch the latest Roblox version from RDD's data source (Roblox clientsettings v2).
        Returns the version string (e.g. 'version-d584fb6c717a43d9'), or None on failure.
        """
        try:
            req = urllib.request.Request(
                RDD_VERSION_URL,
                headers={'User-Agent': 'RobloxStudio/WinInet', 'Accept': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.load(response)
                version = data.get('clientVersionUpload') or data.get('clientVersion')
                if version:
                    return version
        except Exception:
            pass
        return None

    def update_remote_ui(self):
        if getattr(self, "remote_source", None) == "RDD":
            self.remote_ver_label.configure(text=f"{self.remote_version} (RDD)")
        else:
            self.remote_ver_label.configure(text=self.remote_version)
        self.check_roblox_status() # Re-evaluate local vs remote comparison
        
        # Also check launcher updates from user's GitHub
        thread = threading.Thread(target=self.check_launcher_updates_task, daemon=True)
        thread.start()

    def check_launcher_updates_task(self):
        try:
            req = urllib.request.Request(
                REMOTE_VERSION_URL, 
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                launcher_ver = response.read().decode('utf-8').strip()
            
            local_launcher_ver = self.launcher_version
            if local_launcher_ver != launcher_ver:
                self.root_after_safe(lambda: self.on_update_available(launcher_ver))
                self._pending_launcher_version = launcher_ver
            else:
                self.root_after_safe(lambda: self.on_up_to_date())
            # Log launcher update check
            self.log(f"Launcher version checked: {launcher_ver}")
        except Exception:
            pass

    def root_after_safe(self, func):
        try:
            self.after(0, func)
        except Exception:
            pass

    def log(self, message):
        """Append a timestamped line to the in-app log console (thread-safe)."""
        def _append():
            try:
                if not getattr(self, "log_console", None):
                    return
                self.log_console.configure(state="normal")
                self.log_console.insert("end", message + "\n")
                self.log_console.see("end")
                self.log_console.configure(state="disabled")
            except Exception:
                pass
        self.root_after_safe(_append)

    def toggle_fullscreen(self):
        # Toggle between normal window size and fullscreen (covering screen) while overrideredirect is True
        if getattr(self, "_is_fullscreen", False):
            # Restore original size and position
            self.geometry(self._normal_geometry)
            self._is_fullscreen = False
            self.status_indicator.configure(text="Windowed")
        else:
            # Store current geometry before going fullscreen
            self._normal_geometry = self.geometry()
            screen_width = self.winfo_screenwidth()
            screen_height = self.winfo_screenheight()
            self.geometry(f"{screen_width}x{screen_height}+0+0")
            self._is_fullscreen = True
            self.status_indicator.configure(text="Fullscreen")

    def clean_start(self):
        """Terminate any running Roblox processes before launching a fresh instance."""
        try:
            # Attempt to kill common Roblox process names
            subprocess.run(["taskkill", "/f", "/im", "RobloxPlayerBeta.exe"], capture_output=True)
            subprocess.run(["taskkill", "/f", "/im", "RobloxPlayer.exe"], capture_output=True)
            subprocess.run(["taskkill", "/f", "/im", "RobloxStudioBeta.exe"], capture_output=True)
            subprocess.run(["taskkill", "/f", "/im", "RobloxStudio.exe"], capture_output=True)
            self.log("Clean start: terminated existing Roblox processes.")
            self.status_indicator.configure(text="Cleaned")
        except Exception as e:
            self.log(f"Clean start error: {e}")
            self.status_indicator.configure(text="Clean error")

    def detect_roblox_path(self):
        """Detect installed Roblox path via registry or common locations."""
        exe_path = None
        # 1. Try registry (note: not a raw string — single backslashes are correct here)
        try:
            reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Roblox Corporation\Roblox")
            install_dir, _ = winreg.QueryValueEx(reg_key, "InstallDir")
            winreg.CloseKey(reg_key)
            candidate = os.path.join(install_dir, "RobloxPlayerBeta.exe")
            if os.path.isfile(candidate):
                exe_path = candidate
        except Exception:
            pass

        # 2. Walk the Versions directory and locate the actual player executable
        if not exe_path:
            versions_dir = os.path.expandvars(r"%LocalAppData%\Roblox\Versions")
            if os.path.isdir(versions_dir):
                try:
                    for root_dir, _, files in os.walk(versions_dir):
                        if "RobloxPlayerBeta.exe" in files:
                            exe_path = os.path.join(root_dir, "RobloxPlayerBeta.exe")
                            break
                except Exception:
                    pass

        self.roblox_path = exe_path or ""
        self.is_roblox_installed = bool(exe_path)
        if self.is_roblox_installed:
            self.local_status_lbl.configure(text=f"Installed at {os.path.dirname(self.roblox_path)}", text_color=ACCENT_GREEN)
        else:
            self.local_status_lbl.configure(text="Roblox not found", text_color="#c42b2b")

    def is_newer_version(self, local, remote):
        # Simple comparison: if versions differ, treat as newer
        return local != remote

    def on_up_to_date(self):
        self.status_text.configure(text="Updated")
        self.status_dot.configure(text_color=ACCENT_GREEN)
        self.right_card.configure(border_color=CARD_BORDER_GREEN)
        self.status_indicator.configure(text="Ready")
        self.up_to_date_pill.configure(text="✓ Up to Date", fg_color=PILL_BG_GREEN, text_color=ACCENT_GREEN)
        if self.update_action_btn.winfo_ismapped():
            self.update_action_btn.pack_forget()

    def on_update_available(self, remote_version):
        self.status_text.configure(text="Update available")
        self.status_dot.configure(text_color="#ff4d4d")
        self.right_card.configure(border_color=CARD_BORDER_RED)
        self.up_to_date_pill.configure(text="⚠ Outdated", fg_color="#30151d", text_color=ACCENT_PINK)
        self.update_action_btn.pack(fill="x", padx=15, side="bottom", pady=15)

    def on_update_check_failed(self):
        self.status_text.configure(text="Offline / Idle")
        self.status_dot.configure(text_color="#55545a")

    # --- Auto-Updater Logic ---
    def trigger_update_async(self):
        self.update_action_btn.configure(state="disabled", text="Updating...")
        thread = threading.Thread(target=self.download_and_install_update, daemon=True)
        thread.start()

    def download_and_install_update(self):
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, "update.zip")

        self.root_after_safe(lambda: self.progress_bar.pack(side="right", padx=15))
        
        success = self.download_file_with_progress(REMOTE_UPDATE_ZIP_URL, zip_path)
        if not success:
            self.root_after_safe(self.on_update_failed)
            return

        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                extract_target = os.getcwd()
                zip_ref.extractall(extract_target)
            
            new_ver = getattr(self, "_pending_launcher_version", self.launcher_version)
            self.save_launcher_version(new_ver)
            self.root_after_safe(self.on_update_success)
        except Exception:
            self.root_after_safe(self.on_update_failed)
        finally:
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

    def on_update_failed(self):
        self.update_action_btn.configure(state="normal", text="Update Launcher")
        self.progress_bar.pack_forget()
        self.status_text.configure(text="Update failed")

    def on_update_success(self):
        self.progress_bar.pack_forget()
        self.on_up_to_date()
        self.status_text.configure(text="Update completed")

    # --- General App Fetcher logic ---
    def update_apps_status(self):
        for item in self.app_widgets:
            app = item["app"]
            lbl_status = item["lbl_status"]
            btn_action = item["btn_action"]

            installed = False
            installed_path = ""
            for path in app["check_paths"]:
                if os.path.exists(path):
                    installed = True
                    installed_path = path
                    break

            if installed:
                lbl_status.configure(text="Installed", text_color=ACCENT_GREEN)
                btn_action.configure(
                    text="Launch",
                    fg_color=PILL_BG,
                    hover_color="#2b2b30",
                    command=lambda p=installed_path: self.launch_app(p)
                )
            else:
                lbl_status.configure(text="Not installed", text_color="#c42b2b")
                btn_action.configure(
                    text="Download",
                    fg_color=ACCENT_PINK,
                    hover_color="#ff5f7e",
                    command=lambda a=app: self.download_and_install_app_async(a)
                )

    def launch_app(self, path):
        try:
            subprocess.Popen([path], shell=True)
            self.status_text.configure(text=f"Launched app.")
        except Exception as e:
            self.status_text.configure(text=f"Launch error: {e}")

    def download_and_install_app_async(self, app):
        thread = threading.Thread(target=self.download_and_install_app_task, args=(app,), daemon=True)
        thread.start()

    def download_and_install_app_task(self, app):
        self.root_after_safe(lambda: self.status_text.configure(text=f"Downloading {app['name']}..."))
        temp_dir = tempfile.mkdtemp()
        # Save directly to launcher folder or temp using the defined save_as name
        save_name = app.get("save_as", f"{app['name']}_setup.exe")
        dest_path = os.path.join(os.getcwd(), save_name)

        self.root_after_safe(lambda: self.progress_bar.pack(side="right", padx=15))
        
        success = self.download_file_with_progress(app["download_url"], dest_path)
        if not success:
            # Fallback opening browser download page
            self.root_after_safe(lambda: self.status_text.configure(text=f"Direct download failed. Opening browser..."))
            webbrowser.open(app["fallback_download_page"])
            self.root_after_safe(lambda: self.progress_bar.pack_forget())
            return

        self.root_after_safe(lambda: self.status_text.configure(text=f"Running installer for {app['name']}..."))
        try:
            subprocess.Popen([dest_path], shell=True)
        except Exception as e:
            self.root_after_safe(lambda: self.status_text.configure(text=f"Error starting installer: {e}"))

        self.root_after_safe(lambda: self.progress_bar.pack_forget())
        self.root_after_safe(self.update_apps_status)

    # --- Progress-tracked File Downloader ---
    def download_file_with_progress(self, url, dest_path):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as response:
                total_size = int(response.info().get('Content-Length', 0))
                bytes_downloaded = 0
                block_size = 8192

                with open(dest_path, "wb") as f:
                    while True:
                        buffer = response.read(block_size)
                        if not buffer:
                            break
                        f.write(buffer)
                        bytes_downloaded += len(buffer)
                        
                        if total_size > 0:
                            percent = bytes_downloaded / total_size
                            self.root_after_safe(lambda p=percent: self._smooth_progress_lerp(p))
                return True
        except Exception:
            return False


if __name__ == "__main__":
    try:
        app = XPYLauncherApp()
        app.mainloop()
    except Exception as e:
        import traceback
        with open("crash_log.txt", "w") as f:
            f.write(traceback.format_exc())
