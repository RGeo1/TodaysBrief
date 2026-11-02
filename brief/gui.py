"""Tkinter configuration and preview window for 今日幕僚."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from zoneinfo import ZoneInfo

from .config import RulesForm, read_env, read_rules_form, update_env, write_rules_form
from .delivery import SmtpConfig, render, send_brief
from .judgment import analyze, load_rules
from .sources import load_source

if os.name == "nt":
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

def _base_dir() -> Path:
    """Source checkout uses the repo root; frozen app uses the launch cwd."""
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("BRIEF_HOME", Path.cwd()))
    return Path(__file__).resolve().parent.parent


BASE = _base_dir()
TASK_NAME = "TodayBrief"

# Cyberpunk HUD palette: black-dominant, white text, one restrained neon accent.
BG = "#08080a"
PANEL = "#111114"
PANEL_ALT = "#17171c"
TEXT = "#f2f2f2"
MUTED = "#8c8c96"
BORDER = "#2a2a30"
ACCENT = "#fcee0a"
ACCENT_DIM = "#8a8206"
CYAN = "#00e5ff"
CYAN_DIM = "#00707d"
MAGENTA_DIM = "#7d1734"
SCAN = "#141418"
FONT_BODY = ("Microsoft YaHei UI", 10)
FONT_LABEL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 24, "bold")
FONT_SECTION = ("Microsoft YaHei UI", 10, "bold")
FONT_HUD = ("Bahnschrift SemiCondensed", 10, "bold")
FONT_HUD_SMALL = ("Bahnschrift SemiCondensed", 8, "bold")
FONT_MONO = ("Cascadia Mono", 9)
FONT_PREVIEW = ("Microsoft YaHei UI", 10)

HELP_TEXT = """【日历 ICS 链接】
Outlook 网页版：设置 → 日历 → 共享日历 → 发布日历 → 选择有事件的日历 → 权限“可查看所有详细信息” → 发布 → 复制 ICS 链接。
没有“发布日历”时：Outlook 桌面版 → 文件 → 另存为日历 → 保存 .ics，然后在本页选择“本地 .ics 文件”。
Google Calendar：设置 → 选择日历 → “私密地址（iCal 格式）”。
Apple / iCloud：日历 → 共享 → 公开日历 → 复制 webcal 链接，把 webcal:// 改成 https://。

【SMTP 授权码】
QQ 邮箱：mail.qq.com → 设置 → 账户 → 开启 POP3/SMTP 服务 → 生成授权码。
163 邮箱：mail.163.com → 设置 → POP3/SMTP/IMAP → 开启 SMTP → 设置授权密码。
Gmail：myaccount.google.com → 安全性 → 两步验证 → 应用专用密码。
Microsoft 365：smtp.office365.com:587，可能需管理员开启 SMTP AUTH。

授权码 / 应用密码不是登录密码；只写入本机 .env，不会回显。"""


def _smtp_from_env(env: dict[str, str]) -> SmtpConfig:
    return SmtpConfig(
        host=env.get("SMTP_HOST", ""),
        port=int(env.get("SMTP_PORT", "587") or 587),
        sender=env.get("SMTP_FROM", ""),
        recipient=env.get("SMTP_TO", ""),
        user=env.get("SMTP_USER", ""),
        password=env.get("SMTP_PASSWORD", ""),
        tls=env.get("SMTP_TLS", "starttls") or "starttls",
        timeout=int(env.get("SMTP_TIMEOUT", "20") or 20),
    )


class CyberLabelFrame(ttk.LabelFrame):
    """LabelFrame with a HUD spine and a cyan corner marker."""

    def __init__(self, master, text: str = "", **kwargs) -> None:
        super().__init__(master, text=text, **kwargs)
        tk.Frame(self, bg=ACCENT, width=2).place(x=0, y=0, relheight=1.0)
        tk.Label(
            self, text="//", font=FONT_HUD_SMALL, bg=BG, fg=CYAN
        ).place(relx=1.0, rely=0.0, anchor="ne", x=-6, y=2)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("今日幕僚 · 配置")
        try:
            icon_path = BASE / "assets" / "today_brief.ico"
            if icon_path.exists():
                self.iconbitmap(str(icon_path))
        except Exception:
            pass
        self.geometry("960x840")
        self.minsize(900, 720)
        self.base = BASE
        self.env_path = Path(os.environ.get("BRIEF_ENV_FILE", self.base / ".env"))
        self.rules_path = Path(os.environ.get("BRIEF_RULES", self.base / "rules.toml"))
        self.state_dir = Path(os.environ.get("BRIEF_STATE_DIR", self.base / "state"))
        self.out_dir = Path(os.environ.get("BRIEF_OUT_DIR", self.base / "out"))
        self.env: dict[str, str] = {}
        self.password_set = False
        self._build()
        self._reload()

    # ---------- layout ----------

    def _apply_theme(self) -> None:
        self.configure(bg=BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", BG)

        style.configure(".", font=FONT_BODY, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=FONT_BODY)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_LABEL)
        style.configure("Cyan.TLabel", background=BG, foreground=CYAN, font=FONT_HUD_SMALL)
        style.configure(
            "TButton",
            background=PANEL,
            foreground=TEXT,
            bordercolor=BORDER,
            focuscolor=ACCENT,
            relief="flat",
            padding=(10, 6),
            font=FONT_LABEL,
        )
        style.map(
            "TButton",
            background=[("pressed", ACCENT_DIM), ("active", ACCENT)],
            foreground=[("pressed", BG), ("active", BG)],
        )
        style.configure(
            "Primary.TButton",
            background=ACCENT,
            foreground=BG,
            bordercolor=ACCENT,
            focuscolor=ACCENT,
            relief="flat",
            padding=(12, 7),
            font=FONT_HUD,
        )
        style.map(
            "Primary.TButton",
            background=[("pressed", ACCENT_DIM), ("active", "#fff37a")],
            foreground=[("pressed", BG), ("active", BG)],
        )
        style.configure(
            "TEntry",
            fieldbackground=PANEL,
            foreground=TEXT,
            insertcolor=ACCENT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=4,
        )
        style.map(
            "TEntry",
            fieldbackground=[("focus", PANEL_ALT)],
            bordercolor=[("focus", ACCENT)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=PANEL,
            background=PANEL,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor=BORDER,
            padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", ACCENT)],
            selectforeground=[("readonly", BG)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=PANEL,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor=BORDER,
            padding=4,
        )
        style.configure("TRadiobutton", background=BG, foreground=TEXT, font=FONT_BODY)
        style.map("TRadiobutton", background=[("active", BG)], foreground=[("active", ACCENT)])
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background=BG,
            foreground=MUTED,
            padding=(16, 8),
            font=FONT_SECTION,
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", PANEL)],
            foreground=[("selected", ACCENT)],
        )
        style.configure(
            "TLabelframe", background=BG, bordercolor=BORDER, relief="solid", borderwidth=1
        )
        style.configure(
            "TLabelframe.Label", background=BG, foreground=ACCENT, font=FONT_SECTION
        )
        style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            bordercolor=BORDER,
            rowheight=24,
            font=FONT_BODY,
        )
        style.configure(
            "Treeview.Heading", background=BG, foreground=MUTED, font=FONT_LABEL, relief="flat"
        )
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", BG)])
        style.configure(
            "Vertical.TScrollbar",
            background=PANEL,
            troughcolor=BG,
            arrowcolor=MUTED,
            bordercolor=BG,
        )

    def _build(self) -> None:
        self._apply_theme()

        self.header = tk.Canvas(self, height=96, bg=BG, highlightthickness=0)
        self.header.pack(fill="x", padx=14, pady=(12, 0))
        self.header.bind("<Configure>", self._draw_header)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(10, 0))
        tk.Frame(body, bg=ACCENT, width=2).pack(side="left", fill="y")
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(side="left", fill="both", expand=True)

        self._build_source_tab()
        self._build_rules_tab()
        self._build_task_tab()
        self._build_preview_tab()

        bar = ttk.Frame(self, padding=(14, 8))
        bar.pack(fill="x")
        ttk.Button(
            bar, text="保存全部", style="Primary.TButton", command=self._save_clicked
        ).pack(side="left")
        ttk.Button(bar, text="重新加载", command=self._reload).pack(side="left", padx=6)
        ttk.Button(bar, text="退出", command=self.destroy).pack(side="right")
        ttk.Label(
            bar, text="NO CLOUD // NO TELEMETRY", style="Muted.TLabel"
        ).pack(side="right", padx=12)
        ttk.Label(bar, text="SYSTEM READY", style="Cyan.TLabel").pack(side="right")
        tk.Label(
            bar, text="■", font=FONT_HUD_SMALL, bg=BG, fg=CYAN
        ).pack(side="right", padx=(0, 4))

    def _draw_header(self, event=None) -> None:
        canvas = self.header
        canvas.delete("all")
        width = max(canvas.winfo_width(), 800)
        # CRT scanlines
        for y in range(0, 96, 4):
            canvas.create_line(0, y, width, y, fill=SCAN, width=1)
        # HUD corner brackets
        canvas.create_line(0, 0, 30, 0, fill=ACCENT, width=2)
        canvas.create_line(0, 0, 0, 30, fill=ACCENT, width=2)
        canvas.create_line(width, 0, width - 30, 0, fill=ACCENT, width=2)
        canvas.create_line(width, 0, width, 30, fill=ACCENT, width=2)
        canvas.create_line(30, 0, width - 30, 0, fill=CYAN, width=1)
        # Accent spine
        canvas.create_rectangle(0, 18, 5, 68, fill=ACCENT, outline="")
        # Glitch title: dim offsets, white on top
        canvas.create_text(
            14, 10, anchor="nw", text="今日幕僚", font=FONT_TITLE, fill=CYAN_DIM
        )
        canvas.create_text(
            18, 10, anchor="nw", text="今日幕僚", font=FONT_TITLE, fill=MAGENTA_DIM
        )
        canvas.create_text(
            16, 10, anchor="nw", text="今日幕僚", font=FONT_TITLE, fill=TEXT
        )
        canvas.create_text(
            18, 54, anchor="nw", text="// DAILY BRIEF", font=FONT_HUD, fill=ACCENT
        )
        canvas.create_text(
            width - 8,
            14,
            anchor="ne",
            text="SYS.ONLINE",
            font=FONT_HUD_SMALL,
            fill=CYAN,
        )
        canvas.create_text(
            width - 8,
            30,
            anchor="ne",
            text="// LOCAL  ·  NO CLOUD",
            font=FONT_HUD_SMALL,
            fill=MUTED,
        )
        canvas.create_text(
            width - 8,
            46,
            anchor="ne",
            text="NO TELEMETRY  ·  ICAL ONLY  ·  07:00",
            font=FONT_HUD_SMALL,
            fill=MUTED,
        )
        canvas.create_line(0, 92, width, 92, fill=ACCENT, width=2)

    def _build_source_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="01 // 日历与邮箱")
        frame.columnconfigure(0, weight=1)

        source = CyberLabelFrame(frame, text="日历源 // SOURCE", padding=10)
        source.grid(row=0, column=0, sticky="ew")
        source.columnconfigure(1, weight=1)

        self.source_var = tk.StringVar(value="url")
        self.url_var = tk.StringVar()
        self.file_var = tk.StringVar()
        ttk.Radiobutton(source, text="订阅链接", variable=self.source_var, value="url").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(source, textvariable=self.url_var).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Radiobutton(source, text="本地 .ics 文件", variable=self.source_var, value="file").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        file_row = ttk.Frame(source)
        file_row.grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        file_row.columnconfigure(0, weight=1)
        ttk.Entry(file_row, textvariable=self.file_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(file_row, text="浏览...", command=self._browse_ics).grid(
            row=0, column=1, padx=(6, 0)
        )

        smtp = CyberLabelFrame(frame, text="SMTP // MAIL", padding=10)
        smtp.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        smtp.columnconfigure(1, weight=1)
        smtp.columnconfigure(3, weight=1)

        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.tls_var = tk.StringVar()
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.from_var = tk.StringVar()
        self.to_var = tk.StringVar()
        self.timeout_var = tk.StringVar()

        fields = [
            ("服务器", self.host_var, "端口", self.port_var),
            ("登录账号", self.user_var, "加密方式", self.tls_var),
            ("发件人", self.from_var, "收件人", self.to_var),
        ]
        row = 0
        for label1, var1, label2, var2 in fields:
            ttk.Label(smtp, text=label1).grid(row=row, column=0, sticky="w", pady=4)
            if label2 == "加密方式":
                ttk.Combobox(
                    smtp,
                    textvariable=var2,
                    values=["starttls", "ssl", "none"],
                    state="readonly",
                ).grid(row=row, column=3, sticky="ew", padx=6, pady=4)
            else:
                ttk.Entry(smtp, textvariable=var2).grid(
                    row=row, column=3, sticky="ew", padx=6, pady=4
                )
            ttk.Entry(smtp, textvariable=var1).grid(
                row=row, column=1, sticky="ew", padx=6, pady=4
            )
            ttk.Label(smtp, text=label2).grid(row=row, column=2, sticky="w", pady=4)
            row += 1

        ttk.Label(smtp, text="授权码/密码").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(smtp, textvariable=self.password_var, show="*").grid(
            row=row, column=1, sticky="ew", padx=6, pady=4
        )
        self.password_hint = ttk.Label(
            smtp, text="留空不修改；填授权码，不是登录密码", foreground=MUTED
        )
        self.password_hint.grid(row=row, column=2, columnspan=2, sticky="w", pady=4)
        row += 1
        ttk.Label(smtp, text="超时（秒）").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(smtp, textvariable=self.timeout_var).grid(
            row=row, column=1, sticky="ew", padx=6, pady=4
        )

        note = ttk.Label(
            frame,
            text="日历链接和 SMTP 密码只保存在本机 .env；密码不会回显。",
            foreground=MUTED,
        )
        note.grid(row=2, column=0, sticky="w", pady=(10, 0))

        frame.rowconfigure(3, weight=1)
        help_frame = CyberLabelFrame(frame, text="获取方式 // HELP", padding=8)
        help_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        help_frame.columnconfigure(0, weight=1)
        help_frame.rowconfigure(0, weight=1)
        self.help_text = tk.Text(
            help_frame,
            height=11,
            wrap="word",
            font=FONT_PREVIEW,
            bg=PANEL,
            fg=TEXT,
            insertbackground=ACCENT,
            selectbackground=ACCENT,
            selectforeground=BG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        self.help_text.grid(row=0, column=0, sticky="nsew")
        help_scroll = ttk.Scrollbar(
            help_frame, orient="vertical", command=self.help_text.yview
        )
        help_scroll.grid(row=0, column=1, sticky="ns")
        self.help_text.configure(yscrollcommand=help_scroll.set)
        self.help_text.insert("1.0", HELP_TEXT)
        self.help_text.configure(state="disabled")

    def _build_rules_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="02 // 规则")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        general = CyberLabelFrame(frame, text="基础 // CORE", padding=10)
        general.grid(row=0, column=0, sticky="ew")
        general.columnconfigure(1, weight=1)
        general.columnconfigure(3, weight=1)

        self.timezone_var = tk.StringVar()
        self.window_hours_var = tk.StringVar()
        self.max_actions_var = tk.StringVar()
        self.windows_var = tk.StringVar()

        ttk.Label(general, text="时区").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(general, textvariable=self.timezone_var).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4
        )
        ttk.Label(general, text="窗口（小时）").grid(row=0, column=2, sticky="w", pady=4)
        ttk.Entry(general, textvariable=self.window_hours_var).grid(
            row=0, column=3, sticky="ew", padx=6, pady=4
        )
        ttk.Label(general, text="最多行动数").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(general, textvariable=self.max_actions_var).grid(
            row=1, column=1, sticky="ew", padx=6, pady=4
        )
        ttk.Label(general, text="保护时段（逗号分隔）").grid(
            row=1, column=2, sticky="w", pady=4
        )
        ttk.Entry(general, textvariable=self.windows_var).grid(
            row=1, column=3, sticky="ew", padx=6, pady=4
        )

        keywords = CyberLabelFrame(frame, text="关键词权重 // KEYWORDS", padding=10)
        keywords.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        keywords.columnconfigure(0, weight=1)
        keywords.rowconfigure(0, weight=1)

        self.kw_tree = ttk.Treeview(
            keywords, columns=("pattern", "weight"), show="headings", height=6
        )
        self.kw_tree.heading("pattern", text="正则")
        self.kw_tree.heading("weight", text="权重")
        self.kw_tree.column("pattern", width=460)
        self.kw_tree.column("weight", width=60, anchor="center")
        self.kw_tree.grid(row=0, column=0, columnspan=3, sticky="nsew")

        self.kw_pattern_var = tk.StringVar()
        self.kw_weight_var = tk.StringVar(value="4")
        ttk.Entry(keywords, textvariable=self.kw_pattern_var).grid(
            row=1, column=0, sticky="ew", padx=(0, 6), pady=(8, 0)
        )
        ttk.Entry(keywords, textvariable=self.kw_weight_var, width=6).grid(
            row=1, column=1, pady=(8, 0)
        )
        ttk.Button(keywords, text="添加", command=self._add_keyword).grid(
            row=1, column=2, padx=(6, 0), pady=(8, 0)
        )
        ttk.Button(keywords, text="删除选中", command=self._remove_keyword).grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )

        thresholds = CyberLabelFrame(frame, text="阈值 // THRESHOLDS", padding=10)
        thresholds.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        for index in range(4):
            thresholds.columnconfigure(index * 2 + 1, weight=1)

        self.focus_var = tk.StringVar()
        self.salvage_var = tk.StringVar()
        self.tight_var = tk.StringVar()
        self.do_var = tk.StringVar()
        pairs = [
            ("专注块（分钟）", self.focus_var),
            ("可抢救（分钟）", self.salvage_var),
            ("背靠背（分钟）", self.tight_var),
            ("DO 缓冲（分钟）", self.do_var),
        ]
        for index, (label, var) in enumerate(pairs):
            ttk.Label(thresholds, text=label).grid(
                row=0, column=index * 2, sticky="w", pady=4
            )
            ttk.Entry(thresholds, textvariable=var, width=8).grid(
                row=0, column=index * 2 + 1, sticky="ew", padx=6, pady=4
            )

    def _build_task_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="03 // 任务")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        time_frame = CyberLabelFrame(frame, text="发信时间 // SCHEDULE", padding=10)
        time_frame.grid(row=0, column=0, sticky="ew")
        self.brief_hour_var = tk.StringVar()
        ttk.Label(time_frame, text="每天几点发送（0–23）").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            time_frame, from_=0, to=23, textvariable=self.brief_hour_var, width=6
        ).grid(row=0, column=1, sticky="w", padx=8)

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Button(buttons, text="注册 / 更新任务", command=self._register_task).pack(
            side="left"
        )
        ttk.Button(buttons, text="查看任务状态", command=self._task_status).pack(
            side="left", padx=6
        )
        ttk.Button(buttons, text="运行一次", command=self._run_task).pack(side="left")

        ttk.Label(
            frame,
            text="每天 07:00 运行；关机后会在下次登录时补发。任务以当前登录用户运行。",
            foreground=MUTED,
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        self.task_text = tk.Text(
            frame,
            height=12,
            wrap="word",
            font=FONT_MONO,
            bg=PANEL,
            fg=TEXT,
            insertbackground=ACCENT,
            selectbackground=ACCENT,
            selectforeground=BG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        self.task_text.grid(row=3, column=0, sticky="nsew", pady=(10, 0))

    def _build_preview_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="04 // 预览")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=0, sticky="w")
        self.preview_button = ttk.Button(
            buttons, text="保存并预览", command=self._preview
        )
        self.preview_button.pack(side="left")
        self.send_button = ttk.Button(
            buttons,
            text="发送测试邮件",
            style="Primary.TButton",
            command=self._send_test,
        )
        self.send_button.pack(side="left", padx=6)
        ttk.Button(buttons, text="打开输出目录", command=self._open_out).pack(side="left")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(frame, textvariable=self.status_var, foreground=MUTED).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )

        text_frame = ttk.Frame(frame)
        text_frame.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.preview_text = tk.Text(
            text_frame,
            wrap="word",
            font=FONT_PREVIEW,
            state="disabled",
            bg=PANEL,
            fg=TEXT,
            insertbackground=ACCENT,
            selectbackground=ACCENT,
            selectforeground=BG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        self.preview_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.preview_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.preview_text.configure(yscrollcommand=scroll.set)

    # ---------- populate / save ----------

    def _reload(self) -> None:
        self.env = read_env(self.env_path)
        form = read_rules_form(self.rules_path)
        self.password_set = bool(self.env.get("SMTP_PASSWORD"))
        self.password_var.set("")
        self.password_hint.configure(
            text=(
                "已设置；留空不修改（授权码/应用密码，不是登录密码）"
                if self.password_set
                else "尚未设置；填授权码/应用密码，不是登录密码"
            )
        )

        url = self.env.get("CALENDAR_ICS_URL", "")
        file_value = self.env.get("CALENDAR_ICS_FILE", "")
        self.source_var.set("url" if url else ("file" if file_value else "url"))
        self.url_var.set(url)
        self.file_var.set(file_value)

        self.host_var.set(self.env.get("SMTP_HOST", ""))
        self.port_var.set(self.env.get("SMTP_PORT", "587"))
        self.tls_var.set(self.env.get("SMTP_TLS", "starttls") or "starttls")
        self.user_var.set(self.env.get("SMTP_USER", ""))
        self.from_var.set(self.env.get("SMTP_FROM", ""))
        self.to_var.set(self.env.get("SMTP_TO", ""))
        self.timeout_var.set(self.env.get("SMTP_TIMEOUT", "20"))

        self.timezone_var.set(form.timezone)
        self.window_hours_var.set(str(form.window_hours))
        self.brief_hour_var.set(str(form.brief_hour))
        self.max_actions_var.set(str(form.max_actions))
        self.windows_var.set(", ".join(form.protected_windows))
        self.focus_var.set(str(form.focus_block_minutes))
        self.salvage_var.set(str(form.protect_min_salvage_minutes))
        self.tight_var.set(str(form.tight_gap_minutes))
        self.do_var.set(str(form.do_buffer_minutes))

        for item in self.kw_tree.get_children():
            self.kw_tree.delete(item)
        for pattern, weight in form.keywords:
            self.kw_tree.insert("", "end", values=(pattern, weight))

    def _int(self, var: tk.StringVar, name: str) -> int:
        try:
            return int(var.get().strip())
        except ValueError as exc:
            raise ValueError(f"{name} 必须是整数") from exc

    def _collect_form(self) -> RulesForm:
        keywords = [
            (str(self.kw_tree.set(item, "pattern")), int(self.kw_tree.set(item, "weight")))
            for item in self.kw_tree.get_children()
        ]
        windows = [
            window.strip()
            for window in self.windows_var.get().replace("，", ",").split(",")
            if window.strip()
        ]
        return RulesForm(
            timezone=self.timezone_var.get().strip(),
            window_hours=self._int(self.window_hours_var, "窗口小时"),
            brief_hour=self._int(self.brief_hour_var, "发信时间"),
            max_actions=self._int(self.max_actions_var, "最多行动数"),
            protected_windows=windows,
            keywords=keywords,
            focus_block_minutes=self._int(self.focus_var, "专注块分钟"),
            protect_min_salvage_minutes=self._int(self.salvage_var, "可抢救分钟"),
            tight_gap_minutes=self._int(self.tight_var, "背靠背分钟"),
            do_buffer_minutes=self._int(self.do_var, "DO 缓冲分钟"),
        )

    def _save(self, silent: bool = False) -> None:
        form = self._collect_form()
        if self.source_var.get() == "url":
            url = self.url_var.get().strip()
            file_value = ""
        else:
            url = ""
            file_value = self.file_var.get().strip()
        updates = {
            "CALENDAR_ICS_URL": url,
            "CALENDAR_ICS_FILE": file_value,
            "SMTP_HOST": self.host_var.get().strip(),
            "SMTP_PORT": str(self._int(self.port_var, "SMTP 端口")),
            "SMTP_TLS": self.tls_var.get().strip() or "starttls",
            "SMTP_USER": self.user_var.get().strip(),
            "SMTP_FROM": self.from_var.get().strip(),
            "SMTP_TO": self.to_var.get().strip(),
            "SMTP_TIMEOUT": str(self._int(self.timeout_var, "SMTP 超时")),
        }
        password = self.password_var.get()
        if password:
            updates["SMTP_PASSWORD"] = password
        update_env(self.env_path, updates)
        write_rules_form(self.rules_path, form)
        self._reload()
        if not silent:
            messagebox.showinfo("已保存", "配置已写入 .env 和 rules.toml。")

    def _save_clicked(self) -> None:
        try:
            self._save()
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _browse_ics(self) -> None:
        path = filedialog.askopenfilename(
            title="选择导出的 .ics 文件", filetypes=[("iCalendar", "*.ics"), ("所有文件", "*.*")]
        )
        if path:
            self.source_var.set("file")
            self.file_var.set(path)

    def _add_keyword(self) -> None:
        pattern = self.kw_pattern_var.get().strip()
        weight = self.kw_weight_var.get().strip()
        if not pattern or not weight:
            messagebox.showerror("无法添加", "正则和权重都要填。")
            return
        try:
            int(weight)
        except ValueError:
            messagebox.showerror("无法添加", "权重必须是 1–5 的整数。")
            return
        self.kw_tree.insert("", "end", values=(pattern, weight))
        self.kw_pattern_var.set("")

    def _remove_keyword(self) -> None:
        for item in self.kw_tree.selection():
            self.kw_tree.delete(item)

    # ---------- preview / send ----------

    def _set_busy(self, busy: bool, message: str = "就绪") -> None:
        state = "disabled" if busy else "normal"
        self.preview_button.configure(state=state)
        self.send_button.configure(state=state)
        self.status_var.set(message)

    def _show_preview(self, text: str, message: str) -> None:
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state="disabled")
        self._set_busy(False, message)

    def _preview(self) -> None:
        try:
            self._save(silent=True)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._set_busy(True, "正在读取日历...")
        threading.Thread(target=self._preview_worker, daemon=True).start()

    def _preview_worker(self) -> None:
        try:
            env = read_env(self.env_path)
            rules = load_rules(self.rules_path)
            tz = ZoneInfo(rules.timezone)
            now = datetime.now(tz)
            day = now.date()
            url = env.get("CALENDAR_ICS_URL") or None
            path = Path(env["CALENDAR_ICS_FILE"]) if env.get("CALENDAR_ICS_FILE") else None
            loaded = load_source(url=url, path=path, state_dir=self.state_dir, now=now)
            if not loaded.ok:
                raise RuntimeError(loaded.warning or "读取日历失败")
            brief = analyze(loaded.events, rules, day)
            footer = f"抓取于 {now:%H:%M} · 来源：{'订阅' if url else '导出文件'}"
            if loaded.cached:
                footer += " · 缓存"
            if loaded.warning:
                footer += f" · {loaded.warning}"
            rendered = render(brief, day, footer)
            send_brief(
                rendered,
                day,
                _smtp_from_env(env),
                self.state_dir,
                self.out_dir,
                dry_run=True,
            )
            message = f"预览完成：{len(loaded.events)} 个事件，{len(rendered.actions)} 条行动"
            self.after(0, lambda: self._show_preview(rendered.text, message))
        except Exception as exc:
            self.after(0, lambda: self._preview_failed(str(exc)))

    def _preview_failed(self, message: str) -> None:
        self._set_busy(False, "预览失败")
        messagebox.showerror("预览失败", message)

    def _send_test(self) -> None:
        try:
            self._save(silent=True)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        env = read_env(self.env_path)
        recipient = env.get("SMTP_TO", "")
        if not messagebox.askyesno(
            "发送测试邮件", f"将向 {recipient or '（未配置收件人）'} 发送一封测试简报，确定吗？"
        ):
            return
        self._set_busy(True, "正在发送测试邮件...")
        threading.Thread(target=self._send_worker, daemon=True).start()

    def _send_worker(self) -> None:
        try:
            env = read_env(self.env_path)
            smtp = _smtp_from_env(env)
            if not smtp.complete:
                raise RuntimeError("SMTP 配置不完整：需要服务器、发件人和收件人")
            rules = load_rules(self.rules_path)
            tz = ZoneInfo(rules.timezone)
            now = datetime.now(tz)
            day = now.date()
            url = env.get("CALENDAR_ICS_URL") or None
            path = Path(env["CALENDAR_ICS_FILE"]) if env.get("CALENDAR_ICS_FILE") else None
            loaded = load_source(url=url, path=path, state_dir=self.state_dir, now=now)
            if not loaded.ok:
                raise RuntimeError(loaded.warning or "读取日历失败")
            brief = analyze(loaded.events, rules, day)
            footer = f"抓取于 {now:%H:%M} · 来源：{'订阅' if url else '导出文件'}"
            if loaded.cached:
                footer += " · 缓存"
            if loaded.warning:
                footer += f" · {loaded.warning}"
            rendered = render(brief, day, footer)
            result = send_brief(
                rendered,
                day,
                smtp,
                self.state_dir,
                self.out_dir,
                force=True,
            )
            self.after(0, lambda: self._send_done(result))
        except Exception as exc:
            self.after(0, lambda: self._send_failed(str(exc)))

    def _send_done(self, result: str) -> None:
        self._set_busy(False, f"发送结果：{result}")
        if result.startswith("failed"):
            messagebox.showerror("发送失败", result)
        else:
            messagebox.showinfo("发送结果", result)

    def _send_failed(self, message: str) -> None:
        self._set_busy(False, "发送失败")
        messagebox.showerror("发送失败", message)

    # ---------- task actions ----------

    def _run_powershell(self, args: list[str]) -> str:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        return output.strip() or f"exit {completed.returncode}"

    def _show_task_output(self, text: str) -> None:
        self.task_text.delete("1.0", "end")
        self.task_text.insert("1.0", text)

    def _register_task(self) -> None:
        script = self.base / "scripts" / "register-task.ps1"
        output = self._run_powershell(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ]
        )
        self._show_task_output(output)

    def _task_status(self) -> None:
        output = self._run_powershell(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Get-ScheduledTaskInfo -TaskName {TASK_NAME} | Format-List LastRunTime,LastTaskResult,NextRunTime",
            ]
        )
        self._show_task_output(output)

    def _run_task(self) -> None:
        output = self._run_powershell(["schtasks", "/run", "/tn", TASK_NAME])
        self._show_task_output(output or "已触发任务。")

    def _open_out(self) -> None:
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(self.out_dir)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc))


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
