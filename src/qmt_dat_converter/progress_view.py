"""Tk presentation of conversion progress; callers invoke it on the UI thread."""

from collections import Counter
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk


PHASES = {"scan": "扫描源文件", "check": "检查现有输出", "source": "处理源文件",
          "publish": "写入分区", "report": "生成报告"}
RESULTS = {"complete": "完成", "partial": "部分完成", "failed": "失败", "cancelled": "已取消"}
REASONS = {
    "invalid-ohlc": "OHLC 价格关系无效",
    "invalid-ohlc-outside-scope": "范围外 OHLC 价格关系无效",
    "output-missing": "输出分区缺失",
    "output-tampered": "输出分区内容已变化",
    "output-tampered-or-untracked": "输出分区异常或未登记",
    "partition-unpublishable": "分区暂不能写入",
    "unclassified": "证券代码未分类",
    "missing-period-directory": "周期目录缺失",
    "outside-source-directory": "源目录外路径",
    "nonstandard-name": "文件名不符合规则",
    "inaccessible-file": "源文件无法访问",
    "inaccessible-directory": "目录无法访问",
    "code-filter": "不在所选代码内",
}


def _target(issue):
    codes = issue.get("blocking_codes") or []
    if codes:
        result = "、".join(map(str, codes[:2]))
        return result + (f"（共 {len(codes)} 只）" if len(codes) > 2 else "")
    result = str(issue.get("code") or "")
    dates = list(dict.fromkeys(row["date"] for row in issue.get("rows", [])
                               if isinstance(row, dict) and row.get("date")))
    if not dates:
        dates = list(dict.fromkeys(issue.get("days") or []))
    if not dates:
        dates = [issue[key] for key in ("day", "date") if issue.get(key)]
    if dates:
        summary = "、".join(map(str, dates[:2]))
        day_count = max(len(dates), len(issue.get("days") or []))
        if day_count > 2:
            summary += f"（共 {day_count} 天）"
        result = f"{result} {summary}".strip()
    return result or Path(str(issue.get("path") or "")).name


def _reason(reason):
    raw = str(reason)
    code, separator, detail = raw.partition(": ")
    label = REASONS.get(code)
    if label is None:
        return raw
    return f"{label}：{REASONS.get(detail, detail)}" if separator else label


def _duration(seconds):
    value = max(0, int(seconds))
    return f"{value // 60}:{value % 60:02d}"


class ProgressPanel(ttk.Frame):
    def __init__(self, parent, *, open_report=None, max_issues=100):
        super().__init__(parent)
        self.max_issues = max_issues
        self._started = None
        self._phase_started = None
        self._phase = None
        self._bar_mode = "indeterminate"
        self._shown = 0
        self._open_report = open_report
        self.phase_var = tk.StringVar(value="进度")
        self.current_var = tk.StringVar()
        self.elapsed_var = tk.StringVar(value="总耗时 0:00；本阶段 0:00")
        self.counts_var = tk.StringVar(value="复用源文件 0；读取源文件 0\n复用 0 天；新增 0 天；修订 0 天；已写入分区 0")
        self.issue_counts_var = tk.StringVar(value="错误 0；警告 0；排除 0；异常项 0")
        self.exclusion_var = tk.StringVar()
        self.result_var = tk.StringVar()
        self.more_var = tk.StringVar()

        self.columnconfigure(0, weight=1)
        ttk.Label(self, textvariable=self.phase_var).grid(row=0, column=0, sticky="w")
        ttk.Label(self, textvariable=self.current_var).grid(row=1, column=0, sticky="w")
        self.bar = ttk.Progressbar(self, mode="indeterminate", maximum=100)
        self.bar.grid(row=2, column=0, sticky="ew", pady=(5, 3))
        ttk.Label(self, textvariable=self.elapsed_var).grid(row=3, column=0, sticky="w")
        ttk.Label(self, textvariable=self.counts_var).grid(row=4, column=0, sticky="w")
        ttk.Label(self, textvariable=self.issue_counts_var).grid(row=5, column=0, sticky="w")
        ttk.Label(self, textvariable=self.exclusion_var, wraplength=590, justify="left").grid(row=6, column=0, sticky="w")
        ttk.Label(self, textvariable=self.result_var, wraplength=590, justify="left").grid(row=7, column=0, sticky="w")
        issue_frame = ttk.Frame(self)
        issue_frame.grid(row=8, column=0, sticky="nsew", pady=(6, 0))
        issue_frame.columnconfigure(0, weight=1)
        self.issues = ttk.Treeview(issue_frame, columns=("category", "target", "reason"),
                                   show="headings", height=5)
        for name, title, width in (("category", "类别", 70), ("target", "代码 / 日期 / 分区", 360),
                                   ("reason", "原因", 270)):
            self.issues.heading(name, text=title)
            self.issues.column(name, width=width, stretch=name == "reason")
        self.issues.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(issue_frame, orient="vertical", command=self.issues.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.issues.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(issue_frame, orient="horizontal", command=self.issues.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.issues.configure(xscrollcommand=horizontal.set)
        ttk.Label(self, textvariable=self.more_var).grid(row=9, column=0, sticky="w")
        self.report_link = ttk.Button(self, text="打开完整报告", command=open_report, state="disabled")
        self.report_link.grid(row=10, column=0, sticky="w")

    def destroy(self):
        self.bar.stop()
        super().destroy()
        self._open_report = None
        self.bar = None
        self.issues = None
        self.report_link = None
        for name in ("phase_var", "current_var", "elapsed_var", "counts_var", "issue_counts_var",
                     "exclusion_var", "result_var", "more_var"):
            setattr(self, name, None)

    def begin(self, action):
        self._started = time.monotonic()
        self._phase_started = self._started
        self._phase = None
        self._shown = 0
        for item in self.issues.get_children():
            self.issues.delete(item)
        self.phase_var.set("扫描准备中" if action == "scan" else "准备处理")
        self.current_var.set("")
        self.counts_var.set("复用源文件 0；读取源文件 0\n复用 0 天；新增 0 天；修订 0 天；已写入分区 0")
        self.issue_counts_var.set("错误 0；警告 0；排除 0；异常项 0")
        self.exclusion_var.set("")
        self.result_var.set("")
        self.more_var.set("")
        self.report_link.configure(state="disabled")
        self._bar("indeterminate")
        self.tick()

    def _bar(self, mode):
        self.bar.stop()
        self._bar_mode = mode
        self.bar.configure(mode=mode, value=0)
        if mode == "indeterminate":
            self.bar.start(40)

    def tick(self):
        if self._started is None:
            return
        now = time.monotonic()
        self.elapsed_var.set(f"总耗时 {_duration(now - self._started)}；"
                             f"本阶段 {_duration(now - self._phase_started)}")

    def _counts(self, counts):
        reused_sources = counts.get("stat_reused_files", 0) + counts.get("scope_reused_files", 0)
        self.counts_var.set(f"复用源文件 {reused_sources}；读取源文件 {counts.get('content_revalidated_files', 0)}\n"
                            f"复用 {counts.get('reused_days', 0)} 天；新增 {counts.get('new_days', 0)} 天；"
                            f"修订 {counts.get('revised_days', 0)} 天；已写入分区 {counts.get('written_partitions', 0)}")

    def _issue_counts(self, counts):
        self.issue_counts_var.set(f"错误 {counts.get('error', 0)}；警告 {counts.get('warning', 0)}；"
                                  f"排除 {counts.get('excluded', 0)}；异常项 {counts.get('malformed', 0)}")

    def _issue(self, issue):
        if issue.get("category") not in {"error", "warning"}:
            return
        self._shown += 1
        if self._shown > self.max_issues:
            self.more_var.set("仅显示前几条问题；请打开完整报告查看全部。")
            return
        self.issues.insert("", "end", values=("错误" if issue["category"] == "error" else "警告",
                                              _target(issue), _reason(issue.get("reason", ""))))

    def consume(self, event):
        kind = event.get("event")
        if kind == "phase":
            self._phase = event.get("phase")
            self._phase_started = time.monotonic()
            self.phase_var.set(f"第 {event.get('index', 0)}/{event.get('total', 5)} 步："
                               f"{PHASES.get(self._phase, self._phase or '')}")
            self.current_var.set("")
            self._bar("determinate" if self._phase in {"source", "publish"} else "indeterminate")
        elif kind in {"source", "partition"}:
            target = f"{event.get('code', '')} {event.get('period', '')}" if kind == "source" else str(event.get("path", ""))
            self.current_var.set(f"{event.get('index', 0)}/{event.get('total', 0)}  {target.strip()}")
        elif kind in {"source_complete", "partition_complete"}:
            total = event.get("total") or 0
            if total and self._bar_mode == "determinate":
                self.bar.configure(value=100 * event.get("index", 0) / total)
        if kind == "issue":
            self._issue(event.get("issue") or {})
        if "counts" in event:
            self._counts(event["counts"] or {})
        if "issue_counts" in event:
            self._issue_counts(event["issue_counts"] or {})
        self.tick()

    def finish(self, report):
        phase_value = float(self.bar.cget("value")) if self._bar_mode == "determinate" else 0
        self.bar.stop()
        if report.get("status") == "complete":
            self.bar.configure(mode="determinate", value=100)
            self._bar_mode = "determinate"
        else:
            self.bar.configure(mode="determinate", value=phase_value)
            self._bar_mode = "determinate"
        counts = report.get("counts") or {}
        issues = report.get("issues") or []
        self._counts(counts)
        if "issue_counts" in report:
            self._issue_counts(report["issue_counts"] or {})
        else:
            self._issue_counts({category: sum(item.get("category") == category for item in issues)
                                for category in ("error", "warning", "excluded", "malformed")})
        excluded = Counter(_reason(item.get("reason", "")) for item in issues
                           if item.get("category") in {"excluded", "malformed"})
        summary = "；".join(f"{reason} {count}" for reason, count in excluded.most_common(3))
        if len(excluded) > 3:
            summary += "；其他类别见完整报告"
        self.exclusion_var.set(f"未纳入：{summary}" if summary else "")
        for item in self.issues.get_children():
            self.issues.delete(item)
        self._shown = 0
        self.more_var.set("")
        for issue in issues:
            self._issue(issue)
        status = RESULTS.get(report.get("status"), report.get("status", "结束"))
        self.result_var.set(f"{status}；已写入 {counts.get('written_partitions', 0)} 个分区")
        if (report.get("paths") or {}).get("text_report") and self._open_report:
            self.report_link.configure(state="normal")
        if self._started is not None:
            self.tick()
            total = report.get("elapsed_seconds")
            stage = (report.get("timings") or {}).get(self._phase)
            if isinstance(total, (int, float)) or isinstance(stage, (int, float)):
                now = time.monotonic()
                total = total if isinstance(total, (int, float)) else now - self._started
                stage = stage if isinstance(stage, (int, float)) else now - self._phase_started
                self.elapsed_var.set(f"总耗时 {_duration(total)}；本阶段 {_duration(stage)}")
        self._started = None

    def fail(self, reason):
        self.bar.stop()
        if self._bar_mode == "indeterminate":
            self.bar.configure(mode="determinate", value=0)
            self._bar_mode = "determinate"
        self.result_var.set(f"失败：{reason}；已写入分区数未知，请检查输出及报告")
        self._started = None
