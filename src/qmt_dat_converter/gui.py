"""Small Windows Tk front end; engine work stays off the Tk thread."""

import os
from pathlib import Path
from queue import Empty, Queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .engine import run
from .model import Config, ConverterError
from .settings import DEFAULT_CONFIG, load_settings, resolve_local_path, save_settings


PERIODS = {"日线": ("1d",), "分钟线": ("1m",), "日线 + 分钟线": ("1d", "1m")}
MODES = {"增量": "incremental", "全量复查": "full", "日期范围": "range"}


class ConverterApp:
    def __init__(self, root: tk.Tk, runner=run, config_path=None):
        self.root = root
        self.runner = runner
        self.config_path = Path(config_path or DEFAULT_CONFIG).resolve()
        config_error = ""
        try:
            settings = load_settings(self.config_path)
        except ConverterError as exc:
            settings = {"source": "", "output": "", "period": "both"}
            config_error = f"本机配置读取失败，请重新填写并保存：{exc}"
        self.events = Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.last_report = None
        self.closing = False
        self.preview_id = None
        self.form_revision = 0
        self.job_revision = None
        self.source_var = tk.StringVar(value=settings["source"])
        self.output_var = tk.StringVar(value=settings["output"])
        self.period_var = tk.StringVar(value={"1d": "日线", "1m": "分钟线", "both": "日线 + 分钟线"}[settings["period"]])
        self.codes_var = tk.StringVar()
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="增量")
        self.scope_var = tk.StringVar()
        self.status_var = tk.StringVar(value=config_error or "待命；首次使用请填写目录并保存本机配置")

        root.title("QMT DAT 离线转换")
        root.minsize(640, 480)
        root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        self._folder_row(frame, 0, "源目录", self.source_var)
        self._folder_row(frame, 1, "输出目录", self.output_var)
        ttk.Label(frame, text="周期").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Combobox(frame, textvariable=self.period_var, values=list(PERIODS), state="readonly", width=20).grid(
            row=2, column=1, sticky="w", pady=6)
        ttk.Label(frame, text="证券代码").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.codes_var).grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="留空=全部；逗号分隔").grid(row=3, column=2, sticky="w", padx=(8, 0))
        dates = ttk.Frame(frame)
        dates.grid(row=4, column=1, sticky="w", pady=6)
        ttk.Label(frame, text="日期范围").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(dates, textvariable=self.start_var, width=14).pack(side="left")
        ttk.Label(dates, text=" 至 ").pack(side="left")
        ttk.Entry(dates, textvariable=self.end_var, width=14).pack(side="left")
        ttk.Label(dates, text="  YYYYMMDD，可留空").pack(side="left")

        modes = ttk.Frame(frame)
        modes.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 4))
        for label in MODES:
            ttk.Radiobutton(modes, text=label, variable=self.mode_var, value=label).pack(side="left", padx=(0, 16))
        ttk.Label(frame, textvariable=self.scope_var, wraplength=590, justify="left").grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=7)

        commands = ttk.Frame(frame)
        commands.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 12))
        self.convert_button = ttk.Button(commands, text="开始转换", command=lambda: self.start_job("convert"))
        self.convert_button.pack(side="left", padx=(0, 8))
        self.verify_button = ttk.Button(commands, text="核验输出", command=lambda: self.start_job("verify"))
        self.verify_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(commands, text="取消", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(0, 8))
        self.report_button = ttk.Button(commands, text="打开报告", command=self.open_report, state="disabled")
        self.report_button.pack(side="left")
        ttk.Button(commands, text="保存本机配置", command=self.save_config).pack(side="left", padx=(8, 0))
        ttk.Separator(frame).grid(row=8, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Label(frame, textvariable=self.status_var, wraplength=590, justify="left").grid(
            row=9, column=0, columnspan=3, sticky="nw")

        for variable in (self.source_var, self.output_var, self.period_var, self.codes_var,
                         self.start_var, self.end_var, self.mode_var):
            variable.trace_add("write", self._scope_changed)
        self._update_scope()

    def _folder_row(self, frame, row, label, variable):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=6)
        ttk.Entry(frame, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Button(frame, text="选择…", command=lambda: self._choose_folder(variable)).grid(
            row=row, column=2, padx=(8, 0), pady=6)

    def _choose_folder(self, variable):
        chosen = filedialog.askdirectory(parent=self.root, initialdir=variable.get())
        if chosen:
            variable.set(chosen)

    def _update_scope(self):
        period = ", ".join(PERIODS.get(self.period_var.get(), ()))
        self.scope_var.set(f"请求范围：{period}；代码 {self.codes_var.get().strip() or '全部可识别'}；"
                           f"{self.start_var.get().strip() or '不限'} 至 "
                           f"{self.end_var.get().strip() or '不限'}；{self.mode_var.get()}。")

    def _scope_changed(self, *_):
        self.form_revision += 1
        self._update_scope()
        if self.preview_id is not None:
            self.root.after_cancel(self.preview_id)
        self.preview_id = self.root.after(650, self._auto_scan)

    def _auto_scan(self):
        self.preview_id = None
        if (not self.worker and self.source_var.get().strip() and self.output_var.get().strip()
                and Path(resolve_local_path(self.source_var.get(), self.config_path)).is_dir()):
            self.start_job("scan")

    def save_config(self):
        try:
            periods = PERIODS[self.period_var.get()]
            save_settings(self.config_path, source=self.source_var.get(), output=self.output_var.get(),
                          period="both" if len(periods) == 2 else periods[0])
        except (ConverterError, KeyError) as exc:
            self.status_var.set(f"保存失败：{exc}")
            return
        self.status_var.set(f"本机配置已保存：{self.config_path}；日期范围和证券筛选不保存")

    def config_from_form(self, mode=None) -> Config:
        if not self.source_var.get().strip() or not self.output_var.get().strip():
            raise ConverterError("源目录和输出目录不能为空")
        selected_mode = mode or MODES[self.mode_var.get()]
        start, end = self.start_var.get().strip() or None, self.end_var.get().strip() or None
        if selected_mode == "range" and not (start and end):
            raise ConverterError("日期范围模式需要起始和结束日期")
        codes = tuple(part.upper() for part in re.split(r"[,，\s]+", self.codes_var.get().strip()) if part)
        if any(not re.fullmatch(r"\d{6}\.(SH|SZ)", code) for code in codes):
            raise ConverterError("证券代码需使用 600000.SH 格式")
        return Config(Path(resolve_local_path(self.source_var.get(), self.config_path)),
                      Path(resolve_local_path(self.output_var.get(), self.config_path)),
                      periods=PERIODS[self.period_var.get()], start=start, end=end, mode=selected_mode,
                      codes=codes)

    def start_job(self, action):
        if self.worker is not None or self.closing:
            return
        if self.preview_id is not None:
            self.root.after_cancel(self.preview_id)
            self.preview_id = None
        try:
            config = self.config_from_form()
        except (ConverterError, KeyError) as exc:
            self.status_var.set(str(exc))
            return
        self.last_report = None
        self.job_revision = self.form_revision
        self.cancel_event.clear()
        self.report_button.configure(state="disabled")
        self.convert_button.configure(state="disabled")
        self.verify_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set("正在扫描…" if action == "scan" else "正在处理…")

        def work():
            try:
                report = self.runner(config, action=action, progress=self.events.put,
                                     cancel=self.cancel_event.is_set)
                self.events.put(("done", report))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.worker = threading.Thread(target=work, name="qmt-dat-worker", daemon=False)
        self.worker.start()
        self.root.after(50, self._poll)

    def _poll(self):
        finished = False
        try:
            while True:
                event = self.events.get_nowait()
                if isinstance(event, tuple):
                    kind, payload = event
                    finished = True
                    if kind == "done":
                        if payload["action"] == "scan" and self.job_revision != self.form_revision:
                            self.status_var.set("范围已修改，正在重新扫描…")
                        else:
                            self.last_report = payload
                            counts = payload["counts"]
                            self.status_var.set(f"{payload['action']}: {payload['status']}；识别 {counts.get('source_files', 0)} 个源文件；"
                                                f"已写入 {counts.get('written_partitions', 0)} 个分区；问题 {len(payload['issues'])} 条")
                            if payload["action"] == "scan":
                                self.scope_var.set(self.scope_var.get() + f" 已识别 {counts.get('source_files', 0)} 个源文件；"
                                                   f"排除 {sum(item['category'] != 'error' for item in payload['issues'])} 项。")
                            if payload["paths"].get("text_report"):
                                self.report_button.configure(state="normal")
                    else:
                        self.status_var.set(f"处理失败：{payload}")
                elif event.get("event") in {"source", "partition"}:
                    target = event.get("code", event.get("path", ""))
                    self.status_var.set(f"{event['event']} {event['index']}/{event['total']}  {target}")
        except Empty:
            pass
        if finished:
            self.worker.join()
            self.worker = None
            self.cancel_button.configure(state="disabled")
            self.convert_button.configure(state="normal")
            self.verify_button.configure(state="normal")
            if self.closing:
                self.root.destroy()
            elif self.job_revision != self.form_revision and self.preview_id is None:
                self.preview_id = self.root.after(0, self._auto_scan)
        elif self.worker is not None:
            self.root.after(50, self._poll)

    def cancel(self):
        if self.worker is not None:
            self.cancel_event.set()
            self.status_var.set("正在等待当前文件或分区处理结束…")

    def close(self):
        if self.preview_id is not None:
            self.root.after_cancel(self.preview_id)
            self.preview_id = None
        if self.worker is not None:
            self.closing = True
            self.cancel()
        else:
            self.root.destroy()

    def open_report(self):
        if self.last_report and self.last_report["paths"].get("text_report"):
            os.startfile(self.last_report["paths"]["text_report"])


def main() -> None:
    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()
