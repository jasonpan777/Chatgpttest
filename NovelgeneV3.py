#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Novelgene — DeepSeek / SiliconFlow 小说批量生成器（单文件 GUI 并行版 v6.0）
=====================================================================

本版要点（面向更“好用”的交互与多服务商实战）：
- 【每个服务商独立凭据】为 DeepSeek、SiliconFlow.cn、SiliconFlow.com、Custom 分别记住 API Key / Base URL / 最近一次使用的模型；
  切换服务商时自动切换到该服务商已保存的 Key 与配置（你吐槽的点，彻底修好）。
- 【模型列表动态拉取】一键「刷新模型列表」：优先用 HTTP GET /v1/models（可选仅聊天模型 sub_type=chat）；
  若本机未安装 requests，则自动回退为 OpenAI SDK 的 client.models.list()。
- 【更顺手的操作】
  * API Key 显示/隐藏切换按钮；「测试连接」可验证 Key 与 Base URL 是否可用并顺便拉一次模型并填充下拉。
  * Provider 切换时自动静默刷新模型（若有 Key），失败不打断操作。
  * 模型下拉允许手输任意模型 ID（不限制，只提示）。
  * Ctrl+R 刷新模型列表；Enter 聚焦在“模型”下拉时也可刷新。
  * “打开输出目录”按钮；进度区更稳。
- 【文本产出】只保留模型生成的 Markdown 二级标题（程序不再注入 ##）；partial 用 HTML 注释分隔。
- 【稳健性】章节实时落盘、UI 更新在主线程、自动收尾、失败重试、标题保底与任务/单书日志。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import Tk, StringVar, IntVar, BooleanVar
from tkinter import ttk, filedialog, messagebox

# 依赖： pip install --upgrade openai ；若可选安装 requests，则模型过滤更准（/v1/models?sub_type=chat）
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    import requests  # 可选
except Exception:
    requests = None

# =============================
# —— 常量与配置 ——
# =============================
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
SILICONFLOW_BASE_URL_CN = "https://api.siliconflow.cn/v1"
SILICONFLOW_BASE_URL_COM = "https://api.siliconflow.com/v1"

HTTP_TIMEOUT = 18000  # 秒

DEFAULT_PROVIDER = "DeepSeek"  # 可选：DeepSeek / SiliconFlow.cn / SiliconFlow.com / Custom
DEFAULT_MODEL_MAP = {
    "DeepSeek": "deepseek-reasoner",
    "SiliconFlow.cn": "deepseek-ai/DeepSeek-R1",
    "SiliconFlow.com": "deepseek-ai/DeepSeek-R1",
    "Custom": "",
}
# 兜底示例（在线拉取优先）
PROVIDER_PRESETS = {
    "DeepSeek": {
        "base_url": DEEPSEEK_BASE_URL,
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "SiliconFlow.cn": {
        "base_url": SILICONFLOW_BASE_URL_CN,
        "models": [
            "deepseek-ai/DeepSeek-R1",
            "Qwen/Qwen2.5-72B-Instruct",
            "meta-llama/Meta-Llama-3.1-8B-Instruct",
            "InternLM/internlm2_5-7b-chat",
        ],
    },
    "SiliconFlow.com": {
        "base_url": SILICONFLOW_BASE_URL_COM,
        "models": [
            "deepseek-ai/DeepSeek-R1",
            "Qwen/Qwen2.5-72B-Instruct",
            "meta-llama/Meta-Llama-3.1-8B-Instruct",
        ],
    },
    "Custom": {
        "base_url": "",
        "models": [],
    },
}

_WIN_INVALID_CHARS = re.compile(r"[\\/:*?\"<>|]")
_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _config_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        path = os.path.join(base, "Novelgene")
    else:
        path = os.path.join(os.path.expanduser("~"), ".novelgene")
    os.makedirs(path, exist_ok=True)
    return path


CONFIG_PATH = os.path.join(_config_dir(), "config.json")
PERSONA_DIR = os.path.join(_config_dir(), "personas")
os.makedirs(PERSONA_DIR, exist_ok=True)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@dataclass
class Section:
    heading: str
    prompt: str


# =============================
# —— 工具 ——
# =============================

def sanitize_filename_win(name: str, max_len: int = 120) -> str:
    name = name.strip().replace("\n", " ")
    name = _WIN_INVALID_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "未命名小说"
    if name.split(".")[0].upper() in _WIN_RESERVED_NAMES:
        name = f"_{name}"
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "作品"


def unique_path(base_dir: str, filename: str) -> str:
    root, ext = os.path.splitext(filename)
    candidate = os.path.join(base_dir, filename)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(base_dir, f"{root} ({n}){ext}")
        n += 1
    return candidate


def parse_md_sections(md_text: str) -> list[Section]:
    """
    从 Markdown 中解析二级标题及其后内容为段落。若无任何 H2，则视整篇为一个段落。
    —— 仅用于拆分“提示词文件”，不会把这些 H2 注入到成品正文。
    """
    pattern = re.compile(r"(?m)^##[ \t]+(.+?)\s*$")
    matches = list(pattern.finditer(md_text))
    sections: list[Section] = []
    if not matches:
        body = md_text.strip()
        if body:
            sections.append(Section(heading="章节 1", prompt=body))
        return sections
    for idx, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(md_text) else len(md_text)
        block = md_text[start:end].strip()
        sections.append(Section(heading=heading, prompt=block))
    return sections


def _append_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass


# =============================
# —— 通用 LLM 客户端 ——
# =============================
class LLMClient:
    def __init__(self, api_key: str, base_url: str):
        if OpenAI is None:
            raise RuntimeError("缺少 openai 库，请先安装：pip install openai")
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=HTTP_TIMEOUT)

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float | None = None,
        retries: int = 2,
        backoff: float = 2.0,
    ) -> str:
        kwargs = {"model": model, "messages": messages, "stream": False}
        if temperature is not None:
            kwargs["temperature"] = float(temperature)
        attempt = 0
        while True:
            try:
                resp = self.client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            except Exception:
                attempt += 1
                if attempt > retries:
                    raise
                time.sleep(backoff ** attempt)


# =============================
# —— GUI 应用 ——
# =============================
class NovelGeneApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Novelgene — DeepSeek / SiliconFlow 小说批量生成器（并行版 v6.0）")
        self.root.geometry("1180x880")

        # ---- 配置加载与兼容 ----
        cfg = load_config()
        # 旧版字段回收并升级为 providers 映射
        providers_cfg = cfg.get("providers", {})
        for name, preset in PROVIDER_PRESETS.items():
            providers_cfg.setdefault(name, {
                "api_key": "",
                "base_url": preset.get("base_url", ""),
                "model": DEFAULT_MODEL_MAP.get(name, ""),
            })
        # Custom 保留用户自定义
        cfg["providers"] = providers_cfg

        # 当前 provider
        self.provider = StringVar(value=cfg.get("provider", DEFAULT_PROVIDER))
        self._providers_cfg: dict = providers_cfg
        self._last_provider = self.provider.get()

        # 以“当前 provider 的配置”初始化 UI 变量
        cur_p = self._providers_cfg.get(self._last_provider, {})
        self.base_url = StringVar(value=cur_p.get("base_url", PROVIDER_PRESETS.get(self._last_provider, {}).get("base_url", "")))
        self.api_key = StringVar(value=cur_p.get("api_key", ""))
        self.model = StringVar(value=cur_p.get("model", DEFAULT_MODEL_MAP.get(self._last_provider, "")))

        # 其它设置
        self.persona = StringVar(value=cfg.get("persona", ""))
        self.md_path = StringVar(value=cfg.get("md_path", ""))
        self.out_dir = StringVar(value=cfg.get("out_dir", os.getcwd()))
        self.parallel = IntVar(value=max(1, int(cfg.get("parallel", 5))))
        self.force_long = BooleanVar(value=cfg.get("force_long", True))
        self.only_chat_models = BooleanVar(value=cfg.get("only_chat_models", True))
        self.key_visible = BooleanVar(value=False)

        self._stop_flag = threading.Event()
        self._executor: ThreadPoolExecutor | None = None
        self._panels: list[BookPanel] = []

        # 任务跟踪
        self._lock = threading.Lock()
        self._futures = []
        self._remaining = 0
        self._job_dir = ""
        self._job_log_path = ""
        self._book_success: dict[int, bool] = {}
        self._job_start_ts: float | None = None

        self._build_ui()
        self._bind_shortcuts()
        self._wire_traces()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # 顶部（服务商/URL/Key）
        frm_top = ttk.Frame(self.root)
        frm_top.pack(fill="x", **pad)

        ttk.Label(frm_top, text="服务商：").grid(row=0, column=0, sticky="e")
        self.cmb_provider = ttk.Combobox(
            frm_top,
            textvariable=self.provider,
            values=list(PROVIDER_PRESETS.keys()),
            width=18,
            state="readonly",
        )
        self.cmb_provider.grid(row=0, column=1, sticky="w")
        self.cmb_provider.bind("<<ComboboxSelected>>", lambda e: self._on_provider_change())

        ttk.Label(frm_top, text="Base URL：").grid(row=0, column=2, sticky="e")
        self.ent_baseurl = ttk.Entry(frm_top, textvariable=self.base_url, width=44)
        self.ent_baseurl.grid(row=0, column=3, sticky="we")

        ttk.Label(frm_top, text="API Key：").grid(row=0, column=4, sticky="e")
        self.ent_key = ttk.Entry(frm_top, textvariable=self.api_key, show="*", width=32)
        self.ent_key.grid(row=0, column=5, sticky="we")
        self.btn_toggle_key = ttk.Button(frm_top, text="显示", width=6, command=self._toggle_key_visibility)
        self.btn_toggle_key.grid(row=0, column=6, sticky="w", padx=(6, 0))
        self.btn_test = ttk.Button(frm_top, text="测试连接", command=lambda: self._refresh_models(silent=False, test_only=True))
        self.btn_test.grid(row=0, column=7, sticky="w", padx=(6, 0))

        frm_top.columnconfigure(3, weight=1)
        frm_top.columnconfigure(5, weight=1)

        # 参数
        frm_mid = ttk.LabelFrame(self.root, text="参数")
        frm_mid.pack(fill="x", **pad)

        ttk.Label(frm_mid, text="模型：").grid(row=0, column=0, sticky="e")
        self.cmb_model = ttk.Combobox(
            frm_mid,
            textvariable=self.model,
            values=PROVIDER_PRESETS.get(self.provider.get(), {}).get("models", []),
            width=48,
            state="normal",  # 允许手动输入任意模型 ID
        )
        self.cmb_model.grid(row=0, column=1, sticky="w")
        ttk.Button(frm_mid, text="刷新模型列表（Ctrl+R）", command=self._refresh_models).grid(row=0, column=2, sticky="w")
        self.chk_only_chat = ttk.Checkbutton(frm_mid, text="仅显示聊天模型（sub_type=chat）", variable=self.only_chat_models)
        self.chk_only_chat.grid(row=0, column=3, sticky="w")

        ttk.Label(frm_mid, text="人设提示词（system）：").grid(row=1, column=0, sticky="ne")
        ent_persona = ttk.Entry(frm_mid, textvariable=self.persona, width=110)
        ent_persona.grid(row=1, column=1, columnspan=5, sticky="we")
        ttk.Button(frm_mid, text="保存人设为TXT…", command=self.save_persona_txt).grid(row=1, column=6, sticky="w")
        ttk.Button(frm_mid, text="从TXT载入…", command=self.load_persona_txt).grid(row=1, column=7, sticky="w")

        ttk.Label(frm_mid, text="提示词 Markdown 文件：").grid(row=2, column=0, sticky="e")
        ttk.Entry(frm_mid, textvariable=self.md_path).grid(row=2, column=1, columnspan=5, sticky="we")
        ttk.Button(frm_mid, text="浏览…", command=self.choose_md).grid(row=2, column=6, sticky="w")

        ttk.Label(frm_mid, text="输出目录：").grid(row=3, column=0, sticky="e")
        ttk.Entry(frm_mid, textvariable=self.out_dir).grid(row=3, column=1, columnspan=5, sticky="we")
        ttk.Button(frm_mid, text="选择…", command=self.choose_dir).grid(row=3, column=6, sticky="w")
        ttk.Button(frm_mid, text="打开输出目录", command=self._open_out_dir).grid(row=3, column=7, sticky="w")

        ttk.Label(frm_mid, text="并发数/生成本数：").grid(row=4, column=0, sticky="e")
        ttk.Spinbox(frm_mid, from_=1, to=64, textvariable=self.parallel, width=8).grid(row=4, column=1, sticky="w")
        ttk.Checkbutton(frm_mid, text="自动追加“尽量长篇输出”指令", variable=self.force_long).grid(
            row=4, column=2, columnspan=3, sticky="w"
        )
        for i in range(1, 6):
            frm_mid.columnconfigure(i, weight=1)

        # 控制
        frm_btn = ttk.Frame(self.root)
        frm_btn.pack(fill="x", **pad)
        self.btn_start = ttk.Button(frm_btn, text="开始并行生成", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(frm_btn, text="停止", command=self.stop, state=tk.DISABLED)
        self.btn_stop.pack(side="left", padx=10)
        ttk.Button(frm_btn, text="保存设置", command=self._persist_ui_to_cfg).pack(side="right")

        # 每本小说进度（滚动容器）
        frm_books = ttk.LabelFrame(self.root, text="每本小说进度")
        frm_books.pack(fill="both", expand=True, **pad)
        self.canvas = tk.Canvas(frm_books, borderwidth=0, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(frm_books, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # 初始化一次 provider 影响
        self._on_provider_change(initial=True)

    def _bind_shortcuts(self):
        self.root.bind_all("<Control-r>", lambda e: self._refresh_models())
        # 在模型下拉输入回车也刷新
        self.cmb_model.bind("<Return>", lambda e: self._refresh_models())

    def _wire_traces(self):
        # 任意凭据/模型/URL 改动时，写回到当前服务商的缓存映射并保存配置
        def cb(*_):
            self._snapshot_current_provider()
            self._persist_ui_to_cfg()
        self.api_key.trace_add("write", cb)
        self.base_url.trace_add("write", cb)
        self.model.trace_add("write", cb)
        self.provider.trace_add("write", lambda *_: self._persist_ui_to_cfg())
        self.only_chat_models.trace_add("write", lambda *_: self._persist_ui_to_cfg())

    # ---------- 选择/保存 ----------
    def choose_md(self):
        path = filedialog.askopenfilename(
            title="选择 Markdown 提示词文件", filetypes=[("Markdown", "*.md"), ("所有文件", "*.*")]
        )
        if path:
            self.md_path.set(path)
            self._persist_ui_to_cfg()

    def choose_dir(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_dir.set(path)
            self._persist_ui_to_cfg()

    def _open_out_dir(self):
        path = self.out_dir.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showwarning("目录无效", "请先选择有效的输出目录。")
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def save_persona_txt(self):
        text = self.persona.get().strip()
        if not text:
            messagebox.showwarning("空人设", "请输入人设提示词后再保存。")
            return
        initfile = os.path.join(PERSONA_DIR, "persona.txt")
        path = filedialog.asksaveasfilename(
            title="保存人设为 TXT",
            defaultextension=".txt",
            initialdir=PERSONA_DIR,
            initialfile=os.path.basename(initfile),
            filetypes=[("Text", "*.txt")],
        )
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            messagebox.showinfo("已保存", f"人设已保存：\n{path}")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def load_persona_txt(self):
        path = filedialog.askopenfilename(
            title="选择人设TXT", initialdir=PERSONA_DIR, filetypes=[("Text", "*.txt"), ("All", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.persona.set(f.read())
            self._persist_ui_to_cfg()
        except Exception as e:
            messagebox.showerror("读取失败", str(e))

    # ---------- 配置 ----------
    def _persist_ui_to_cfg(self):
        # 把 providers 映射 + 当前 UI 其他参数持久化
        cfg = {
            "provider": self.provider.get().strip(),
            "providers": self._providers_cfg,
            "persona": self.persona.get(),
            "md_path": self.md_path.get().strip(),
            "out_dir": self.out_dir.get().strip(),
            "parallel": int(self.parallel.get()),
            "force_long": bool(self.force_long.get()),
            "only_chat_models": bool(self.only_chat_models.get()),
        }
        save_config(cfg)

    def _snapshot_current_provider(self):
        name = self.provider.get()
        entry = self._providers_cfg.setdefault(name, {})
        entry["api_key"] = self.api_key.get().strip()
        entry["base_url"] = self.base_url.get().strip()
        entry["model"] = self.model.get().strip()

    # ---------- Provider 切换 ----------
    def _on_provider_change(self, initial: bool = False):
        # 先把“上一个”provider 的 UI 值写回映射
        if not initial:
            self._snapshot_current_provider()

        name = self.provider.get()
        preset = PROVIDER_PRESETS.get(name, {})
        entry = self._providers_cfg.get(name, {})
        # 切换 UI：用该 provider 已保存的 key / url / model（若无则取 preset / 默认）
        self.base_url.set(entry.get("base_url", preset.get("base_url", "")))
        self.api_key.set(entry.get("api_key", ""))
        recent_model = entry.get("model", "") or DEFAULT_MODEL_MAP.get(name, "")
        self.model.set(recent_model)

        # 兜底展示模型候选
        self.cmb_model.configure(values=preset.get("models", []))

        # 自动尝试在线拉取（有 Key 时）；失败静默
        if self.api_key.get().strip():
            self._refresh_models(silent=True)

        self._last_provider = name
        self._persist_ui_to_cfg()

    # ---------- 拉取模型列表（/v1/models） ----------
    def _refresh_models(self, silent: bool = False, test_only: bool = False):
        """
        在后台线程通过 OpenAI 兼容接口 /v1/models 拉取可用模型列表，并更新下拉。
        优先使用 requests 以支持 ?sub_type=chat 过滤；没有 requests 则回退为 SDK 的 models.list()
        """
        def worker():
            mids = None
            err = None
            used_http = False
            try:
                base = self.base_url.get().strip().rstrip("/")
                ak = self.api_key.get().strip()
                headers = {"Authorization": f"Bearer {ak}"} if ak else {}
                params = {}

                if requests is not None:
                    # HTTP 直连更灵活（支持 sub_type 过滤）
                    used_http = True
                    if self.only_chat_models.get():
                        params["sub_type"] = "chat"
                    url = f"{base}/models" if base else "/models"
                    r = requests.get(url, headers=headers, params=params, timeout=30)
                    r.raise_for_status()
                    data = r.json() or {}
                    items = data.get("data") or []
                    mids = []
                    for it in items:
                        mid = it.get("id") if isinstance(it, dict) else None
                        if mid:
                            mids.append(str(mid))
                    if mids:
                        mids = sorted(set(mids), key=str.lower)
                else:
                    # 回退到 SDK
                    if OpenAI is None:
                        raise RuntimeError("缺少 openai 库或 requests 库，无法拉取模型列表。")
                    cli = OpenAI(api_key=ak, base_url=base, timeout=HTTP_TIMEOUT)
                    resp = cli.models.list()
                    items = getattr(resp, "data", []) or []
                    mids = []
                    for it in items:
                        mid = getattr(it, "id", None)
                        if not mid and isinstance(it, dict):
                            mid = it.get("id")
                        if mid:
                            mids.append(str(mid))
                    if mids:
                        mids = sorted(set(mids), key=str.lower)
            except Exception as e:
                err = e
                mids = None

            def apply():
                if mids:
                    self.cmb_model.configure(values=mids)
                    cur = self.model.get().strip()
                    if not cur or cur not in mids:
                        # 若当前选择不在拉取集合中，则回退到默认或首项
                        default = DEFAULT_MODEL_MAP.get(self.provider.get(), mids[0])
                        self.model.set(default if default in mids else mids[0])
                    if not silent:
                        if test_only:
                            way = "HTTP" if used_http else "SDK"
                            messagebox.showinfo("测试成功", f"连接可用（{way}）。共加载 {len(mids)} 个模型。")
                        else:
                            messagebox.showinfo("模型列表已刷新", f"共加载 {len(mids)} 个模型。")
                else:
                    if not silent:
                        msg = "未能获取模型列表；请检查 API Key / Base URL。\n\n"
                        if err:
                            msg += f"{err}"
                        messagebox.showwarning("拉取失败", msg)
            self._ui(apply)

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 启停 ----------
    def start(self):
        if not self.api_key.get().strip():
            messagebox.showwarning("缺少 API Key", "请先粘贴 API Key（每个服务商各自保存）。")
            return
        if OpenAI is None:
            messagebox.showwarning("缺少依赖", "未检测到 openai 库，请先执行：pip install openai")
            return
        if not self.base_url.get().strip():
            messagebox.showwarning("缺少 Base URL", "请填写 Base URL。")
            return
        if not self.model.get().strip():
            messagebox.showwarning("缺少模型 ID", "请选择或输入模型 ID（可点“刷新模型列表”获取）。")
            return
        if not self.md_path.get().strip() or not os.path.isfile(self.md_path.get().strip()):
            messagebox.showwarning("未选择文件", "请选择 Markdown 提示词文件。")
            return
        if not self.out_dir.get().strip() or not os.path.isdir(self.out_dir.get().strip()):
            messagebox.showwarning("无效输出目录", "请选择有效的输出目录。")
            return

        # 解析提示词
        with open(self.md_path.get().strip(), "r", encoding="utf-8-sig") as f:
            md_text = f.read()
        sections = parse_md_sections(md_text)
        if not sections:
            messagebox.showwarning("无有效提示词", "未在 Markdown 中找到任何二级标题或正文内容。")
            return

        # 固化配置
        self._snapshot_current_provider()
        self._persist_ui_to_cfg()
        self._stop_flag.clear()
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)

        # 清理旧面板
        for p in self._panels:
            p.destroy()
        self._panels.clear()

        # 任务文件夹（到分钟）
        job_dirname = datetime.now().strftime("%Y%m%d-%H%M")
        job_dir = os.path.join(self.out_dir.get().strip(), job_dirname)
        os.makedirs(job_dir, exist_ok=True)
        self._job_dir = job_dir
        self._job_log_path = os.path.join(job_dir, "job-log.md")
        self._book_success.clear()
        self._job_start_ts = time.time()

        # 任务头日志
        _append_text(self._job_log_path, f"# 任务日志 {job_dirname}\n")
        _append_text(self._job_log_path, f"- 服务商: {self.provider.get().strip()}")
        _append_text(self._job_log_path, f"- Base URL: {self.base_url.get().strip()}")
        _append_text(self._job_log_path, f"- 模型: {self.model.get().strip()}")
        _append_text(self._job_log_path, f"- 并发: {int(self.parallel.get())}")
        _append_text(self._job_log_path, f"- 提示词: {self.md_path.get().strip()}")
        _append_text(self._job_log_path, f"- 输出目录: {self.out_dir.get().strip()}")
        _append_text(self._job_log_path, f"- 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        # 面板创建 & 执行
        total = max(1, int(self.parallel.get()))
        for i in range(1, total + 1):
            panel = BookPanel(self.inner, index=i, total_rounds=len(sections))
            panel.pack(fill="x", padx=8, pady=6)
            self._panels.append(panel)

        # 客户端
        try:
            client = LLMClient(self.api_key.get().strip(), self.base_url.get().strip())
        except Exception as e:
            messagebox.showerror("初始化失败", str(e))
            self.btn_start.configure(state=tk.NORMAL)
            self.btn_stop.configure(state=tk.DISABLED)
            return

        # 提交任务并跟踪
        self._executor = ThreadPoolExecutor(max_workers=total)
        self._remaining = total
        self._futures.clear()

        snap = {
            "persona": self.persona.get(),
            "model": self.model.get(),
            "sections": sections,
            "force_long": bool(self.force_long.get()),
            "job_dir": job_dir,
        }

        for panel in self._panels:
            fut = self._executor.submit(self._run_one_book, client, panel, snap)
            fut.add_done_callback(self._on_future_done)
            self._futures.append(fut)

    def stop(self):
        self._stop_flag.set()
        messagebox.showinfo("停止", "将尝试在当前请求结束后停止进行中的任务。")

    def _on_close(self):
        self._snapshot_current_provider()
        self._persist_ui_to_cfg()
        self.stop()
        self.root.destroy()

    # ---------- UI 辅助 ----------
    def _ui(self, fn, *args, **kwargs):
        self.root.after(0, lambda: fn(*args, **kwargs))

    def _on_future_done(self, _fut):
        with self._lock:
            self._remaining -= 1
            done = (self._remaining == 0)
        if done:
            self.root.after(0, self._on_all_done)

    def _on_all_done(self):
        # 自动关停线程池、恢复按钮
        if self._executor:
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass
            self._executor = None
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)

        # 计算总用时（分钟）
        elapsed_min = None
        if self._job_start_ts is not None:
            elapsed_min = (time.time() - self._job_start_ts) / 60.0

        _append_text(self._job_log_path, "\n---\n**任务完成**（保留模型生成的 H2；未执行 H2 清理）")
        if elapsed_min is not None:
            _append_text(self._job_log_path, f"- 总用时：{elapsed_min:.2f} 分钟")
        _append_text(self._job_log_path, f"- 结束时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        # 弹窗提示包含总用时
        if elapsed_min is not None:
            messagebox.showinfo("完成", f"本次任务已结束：\n{self._job_dir}\n总用时：{elapsed_min:.2f} 分钟")
        else:
            messagebox.showinfo("完成", f"本次任务已结束：\n{self._job_dir}")

    # ---------- 单本线程 ----------
    def _run_one_book(self, client: "LLMClient", panel: "BookPanel", snap: dict):
        idx = panel.index
        book_dir = os.path.join(snap["job_dir"], f"book-{idx:03d}")
        partial_path = os.path.join(book_dir, f"book-{idx:03d}.partial.md")
        book_log = os.path.join(book_dir, f"book-{idx:03d}.log.md")

        def log(line: str):
            ts = datetime.now().strftime("%H:%M:%S")
            _append_text(book_log, f"[{ts}] {line}")

        success = False
        out_path = ""

        try:
            if self._stop_flag.is_set():
                self._ui(panel.set_status, "已取消")
                log("任务在启动前被取消")
                return

            messages: list[dict] = []
            persona = (snap.get("persona") or "").strip()
            if persona:
                messages.append({"role": "system", "content": persona})

            sections: list[Section] = snap["sections"]
            model = snap["model"]
            force_long = snap["force_long"]

            outputs: list[str] = []
            self._ui(panel.set_status, "进行中…")
            log("开始生成")

            # 清空/创建 partial
            os.makedirs(book_dir, exist_ok=True)
            open(partial_path, "w", encoding="utf-8").close()

            for i, sec in enumerate(sections, start=1):
                if self._stop_flag.is_set():
                    self._ui(panel.set_status, "已取消")
                    log(f"在第 {i} 轮前取消")
                    return

                user_content = sec.prompt
                if force_long:
                    user_content = (
                        "【写作指令】请基于下列提示进行长篇创作，力求细节丰满、情节连贯、对话自然，\n"
                        "尽可能输出更多的内容，不要保留大纲或提要，不要等待下一条提示，\n"
                        "必要时自主补全缺失的信息与伏笔，保持统一世界观与风格。\n\n"
                        + user_content
                    )
                messages.append({"role": "user", "content": user_content})
                log(f"第 {i}/{len(sections)} 轮：开始")
                t0 = time.time()
                content = ""
                try:
                    content = client.chat(messages, model=model, temperature=0.9)
                except Exception as e:
                    err = f"第 {i} 轮失败：{e}\n{traceback.format_exc()}"
                    self._ui(panel.set_status, f"第{i}轮失败")
                    log(err)
                    # 保持空章节继续
                finally:
                    messages.append({"role": "assistant", "content": content})
                    outputs.append((content or "").strip())

                    # 实时落盘（章节）
                    # 不写入 H2 模板标题；仅保留模型文本。写入一个 HTML 注释分隔符。
                    ch_heading = sec.heading or f"章节 {i}"
                    _append_text(partial_path, f"<!-- section: {ch_heading} -->\n{content}\n")
                    elapsed = time.time() - t0
                    log(f"第 {i} 轮结束：{len(content)} 字，用时 {elapsed:.1f}s")
                    self._ui(panel.set_progress, i, len(sections))

            # 组合正文：从 partial 读回（保证一致）
            with open(partial_path, "r", encoding="utf-8") as f:
                novel_body = f.read().strip()

            # 不做任何 H2 清理，确保仅保留模型生成的二级标题。

            # 生成书名（失败有保底）
            try:
                title_messages = [
                    {"role": "system", "content": "你是严格的书名命名器，只输出最终标题。"},
                    {
                        "role": "user",
                        "content": (
                            "请根据下列小说正文生成一个可市场化的中文书名。要求：\n"
                            "1）只输出标题本身；2）不含书名号/引号/标点；3）不超过25个汉字或40个字符。\n\n"
                            "【小说正文】\n" + novel_body
                        ),
                    },
                ]
                raw_title = client.chat(title_messages, model=model, temperature=0.7)
            except Exception as e:
                raw_title = ""
                log(f"书名生成失败：{e}\n{traceback.format_exc()}")

            title_raw = (raw_title or "").strip() or f"未命名小说-{idx}-{int(time.time())}"
            title = sanitize_filename_win(title_raw)
            out_path = unique_path(snap["job_dir"], f"{title}.md")

            with open(out_path, "w", encoding="utf-8") as f:
                f.write(f"# {title_raw}\n\n")
                f.write(novel_body)
                f.write("\n")

            self._ui(panel.set_done, out_path)
            log(f"完成，成品：{out_path}")
            _append_text(self._job_log_path, f"- book-{idx:03d} 完成：{os.path.basename(out_path)}")
            success = True

        except Exception as e:
            msg = f"异常：{e}"
            self._ui(panel.set_status, msg)
            log(msg + "\n" + traceback.format_exc())
            _append_text(self._job_log_path, f"- book-{idx:03d} 异常：{e}")
        finally:
            # 记录该书是否成功产出成品 .md
            with self._lock:
                self._book_success[idx] = bool(success and out_path and os.path.exists(out_path))

    # ---------- 小工具 ----------
    def _toggle_key_visibility(self):
        self.key_visible.set(not self.key_visible.get())
        self.ent_key.configure(show="" if self.key_visible.get() else "*")
        self.btn_toggle_key.configure(text="隐藏" if self.key_visible.get() else "显示")


# =============================
# —— 每本小说的进度面板 ——
# =============================
class BookPanel(ttk.Frame):
    def __init__(self, master, index: int, total_rounds: int):
        super().__init__(master)
        self.index = index
        self.total_rounds = total_rounds
        self._progress = IntVar(value=0)
        self._status = StringVar(value="等待中")
        self._path = StringVar(value="")

        ttk.Label(self, text=f"第 {index} 本", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(self, textvariable=self._status).grid(row=0, column=1, sticky="w", padx=8)

        self.bar = ttk.Progressbar(self, maximum=max(1, total_rounds), variable=self._progress)
        self.bar.grid(row=1, column=0, columnspan=2, sticky="we", pady=4)
        self.lbl_prog = ttk.Label(self, text=f"0/{total_rounds}")
        self.lbl_prog.grid(row=1, column=2, sticky="e", padx=8)

        ttk.Label(self, textvariable=self._path).grid(row=2, column=0, columnspan=3, sticky="w")
        self.columnconfigure(1, weight=1)

    def set_progress(self, cur: int, total: int):
        self._progress.set(cur)
        self.bar.configure(maximum=max(1, total))
        self.lbl_prog.configure(text=f"{cur}/{total}")
        self._status.set("进行中…")

    def set_status(self, text: str):
        self._status.set(text)

    def set_done(self, path: str):
        self._progress.set(self.total_rounds)
        self.lbl_prog.configure(text=f"{self.total_rounds}/{self.total_rounds}")
        self._status.set("✅ 完成")
        self._path.set(path)


# =============================
# —— 入口 ——
# =============================
def main():
    root = Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    app = NovelGeneApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
