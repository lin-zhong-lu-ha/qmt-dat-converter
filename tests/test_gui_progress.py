import gc
import threading
import time
import tkinter as tk
import weakref

import pytest

from qmt_dat_converter.gui import ConverterApp
from qmt_dat_converter.progress_view import ProgressPanel


def pump(root, condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("GUI did not reach expected state")


def test_phase_progress_is_scoped_to_known_length_stage():
    root = tk.Tk()
    try:
        panel = ProgressPanel(root)
        panel.pack()
        panel.begin("convert")
        panel.consume({"event": "phase", "phase": "scan", "index": 1, "total": 5})
        assert str(panel.bar.cget("mode")) == "indeterminate"
        panel.consume({"event": "phase", "phase": "source", "index": 3, "total": 5})
        panel.consume({"event": "source", "index": 2, "total": 4, "code": "600000.SH", "period": "1m"})
        panel.consume({"event": "source_complete", "index": 2, "total": 4,
                       "counts": {"new_days": 2, "revised_days": 1, "reused_days": 3,
                                  "stat_reused_files": 1, "scope_reused_files": 1,
                                  "content_revalidated_files": 2, "written_partitions": 0},
                       "issue_counts": {"error": 0, "warning": 1, "excluded": 1298}})
        assert str(panel.bar.cget("mode")) == "determinate"
        assert float(panel.bar.cget("value")) == 50
        assert "第 3/5 步" in panel.phase_var.get()
        assert "600000.SH" in panel.current_var.get()
        assert "已写入分区 0" in panel.counts_var.get()
        assert "复用源文件 2" in panel.counts_var.get()
        assert "错误 0" in panel.issue_counts_var.get()
        assert "排除 1298" in panel.issue_counts_var.get()
    finally:
        panel.bar.stop()
        root.destroy()


def test_live_issue_visible_before_runner_finishes(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def runner(config, action, progress, cancel):
        progress({"event": "issue", "issue": {"category": "error", "code": "600000.SH",
                                             "reason": "invalid-ohlc", "count": 1,
                                             "days": ["20260930"],
                                             "rows": [{"record": 0, "date": "2026-09-30",
                                                       "datetime": "2026-09-30 15:00:00"}]},
                  "issue_counts": {"error": 1, "warning": 0,
                                   "excluded": 1296, "malformed": 2}})
        entered.set()
        assert release.wait(5)
        return {"action": action, "status": "partial", "counts": {"written_partitions": 0},
                "issues": [], "paths": {}}

    root = tk.Tk()
    try:
        app = ConverterApp(root, runner=runner)
        app.source_var.set(str(tmp_path))
        app.output_var.set(str(tmp_path / "out"))
        app.start_job("convert")
        pump(root, lambda: entered.is_set() and len(app.progress_panel.issues.get_children()) == 1)
        assert app.worker.is_alive()
        row = app.progress_panel.issues.item(app.progress_panel.issues.get_children()[0], "values")
        assert "600000.SH" in row[1] and "2026-09-30" in row[1]
        assert "OHLC" in row[2]
        assert "错误 1" in app.progress_panel.issue_counts_var.get()
        assert "排除 1296" in app.progress_panel.issue_counts_var.get()
        assert "异常项 2" in app.progress_panel.issue_counts_var.get()
    finally:
        release.set()
        pump(root, lambda: app.worker is None)
        root.destroy()


def test_cancelled_report_shows_committed_count_even_when_zero():
    root = tk.Tk()
    try:
        panel = ProgressPanel(root)
        panel.pack()
        panel.begin("convert")
        panel.finish({"action": "convert", "status": "cancelled", "counts": {"written_partitions": 0},
                      "issues": [], "paths": {}})
        assert "已取消" in panel.result_var.get()
        assert "已写入 0 个分区" in panel.result_var.get()
        panel.begin("convert")
        panel.finish({"action": "convert", "status": "partial", "counts": {"written_partitions": 2},
                      "issues": [], "paths": {}})
        assert "部分完成" in panel.result_var.get()
        assert "已写入 2 个分区" in panel.result_var.get()
    finally:
        root.destroy()


def test_issue_table_is_bounded_and_links_to_full_report():
    root = tk.Tk()
    try:
        panel = ProgressPanel(root, max_issues=2)
        panel.pack()
        panel.begin("convert")
        report = {"action": "convert", "status": "partial", "counts": {},
                  "issues": [{"category": "error", "reason": str(i)} for i in range(3)],
                  "paths": {"text_report": "report.txt"}}
        panel.finish(report)
        assert len(panel.issues.get_children()) == 2
        assert "完整报告" in panel.more_var.get()
    finally:
        root.destroy()


def test_engine_issue_rows_show_bounded_dates_and_blocking_stocks():
    root = tk.Tk()
    try:
        panel = ProgressPanel(root)
        panel.pack()
        panel.begin("convert")
        panel.consume({"event": "issue", "issue": {
            "category": "warning", "reason": "invalid-ohlc-outside-scope", "code": "600000.SH",
            "count": 4, "days": ["19940404", "19940405", "19940406"],
            "rows": [{"date": "1994-04-04"}, {"date": "1994-04-05"},
                     {"date": "1994-04-06"}, {"date": "1994-04-06"}]}})
        panel.consume({"event": "issue", "issue": {
            "category": "error", "reason": "partition-unpublishable",
            "path": "data/qmt-daily-monthly/2026/09/SH.parquet",
            "blocking_codes": ["600000.SH", "601000.SH", "601001.SH"]}})
        panel.consume({"event": "issue", "issue": {
            "category": "error", "reason": "output-missing", "path": "data/missing.parquet"}})
        rows = [panel.issues.item(item, "values") for item in panel.issues.get_children()]
        assert "600000.SH" in rows[0][1]
        assert "1994-04-04" in rows[0][1]
        assert "1994-04-05" in rows[0][1]
        assert "共 3 天" in rows[0][1]
        assert "范围外" in rows[0][2]
        assert "600000.SH" in rows[1][1] and "601000.SH" in rows[1][1]
        assert "共 3 只" in rows[1][1]
        assert "分区" in rows[1][2]
        assert "输出分区缺失" in rows[2][2]
    finally:
        root.destroy()


def test_legacy_issue_count_and_unknown_reason_keep_raw_detail():
    root = tk.Tk()
    try:
        panel = ProgressPanel(root)
        panel.pack()
        panel.finish({"status": "partial", "counts": {}, "paths": {},
                      "issues": [{"category": "excluded", "reason": "unclassified"},
                                 {"category": "malformed", "reason": "nonstandard-name"},
                                 {"category": "warning", "reason": "custom-rule"}]})
        assert "排除 1" in panel.issue_counts_var.get()
        assert "异常项 1" in panel.issue_counts_var.get()
        assert "证券代码未分类 1" in panel.exclusion_var.get()
        assert "文件名不符合规则 1" in panel.exclusion_var.get()
        rows = panel.issues.get_children()
        assert len(rows) == 1
        assert "custom-rule" in panel.issues.item(rows[0], "values")[2]
    finally:
        root.destroy()


def test_final_elapsed_uses_saved_report_timings_and_freezes():
    root = tk.Tk()
    try:
        panel = ProgressPanel(root)
        panel.pack()
        panel.begin("convert")
        panel.consume({"event": "phase", "phase": "report", "index": 5, "total": 5})
        panel.finish({"status": "complete", "counts": {}, "issues": [], "paths": {},
                      "elapsed_seconds": 8.406, "timings": {"report": 2.3}})
        assert "总耗时 0:08" in panel.elapsed_var.get()
        assert "本阶段 0:02" in panel.elapsed_var.get()
        frozen = panel.elapsed_var.get()
        panel.tick()
        assert panel.elapsed_var.get() == frozen
    finally:
        root.destroy()


def test_final_bar_only_reaches_full_for_complete_report():
    root = tk.Tk()
    try:
        panel = ProgressPanel(root)
        panel.pack()
        panel.begin("convert")
        panel.consume({"event": "phase", "phase": "report", "index": 5, "total": 5})
        panel.finish({"status": "complete", "counts": {}, "issues": [], "paths": {}})
        assert str(panel.bar.cget("mode")) == "determinate"
        assert float(panel.bar.cget("value")) == 100

        panel.begin("convert")
        panel.consume({"event": "phase", "phase": "report", "index": 5, "total": 5})
        panel.finish({"status": "cancelled", "counts": {}, "issues": [], "paths": {}})
        assert str(panel.bar.cget("mode")) == "determinate"
        assert float(panel.bar.cget("value")) == 0

        panel.begin("convert")
        panel.consume({"event": "phase", "phase": "source", "index": 3, "total": 5})
        panel.consume({"event": "source_complete", "index": 1, "total": 2})
        panel.finish({"status": "partial", "counts": {}, "issues": [], "paths": {}})
        assert float(panel.bar.cget("value")) == 50
    finally:
        root.destroy()


@pytest.mark.parametrize("status", ["cancelled", "failed", "partial"])
def test_unsuccessful_scan_clears_preview_and_remains_eligible(tmp_path, status):
    calls = []

    def runner(config, action, progress, cancel):
        calls.append(action)
        state = status if len(calls) == 2 else "complete"
        return {"action": action, "status": state, "counts": {"source_files": 7},
                "issues": [], "paths": {}}

    root = tk.Tk()
    try:
        app = ConverterApp(root, runner=runner)
        app.source_var.set(str(tmp_path))
        app.output_var.set(str(tmp_path / "out"))
        app.start_job("scan")
        pump(root, lambda: app.worker is None)
        assert "已识别 7 个源文件" in app.scope_var.get()

        app.start_job("scan")
        pump(root, lambda: app.worker is None)
        assert app.last_report["status"] == status
        assert "已识别 7 个源文件" not in app.scope_var.get()
        assert app.preview_id is None
        assert calls == ["scan", "scan"]

        app._auto_scan()
        pump(root, lambda: app.worker is None and len(calls) == 3)
        assert app.last_report["status"] == "complete"
        assert "已识别 7 个源文件" in app.scope_var.get()
    finally:
        root.destroy()


def test_mode_only_change_reuses_completed_scan_preview(tmp_path):
    calls = []

    def runner(config, action, progress, cancel):
        calls.append(action)
        return {"action": action, "status": "complete", "counts": {"source_files": 7},
                "issues": [], "paths": {}}

    root = tk.Tk()
    try:
        app = ConverterApp(root, runner=runner)
        app.source_var.set(str(tmp_path))
        app.output_var.set(str(tmp_path / "out"))
        app.start_job("scan")
        pump(root, lambda: app.worker is None)
        assert "已识别 7 个源文件" in app.scope_var.get()
        app.mode_var.set("全量复查")
        assert "全量复查" in app.scope_var.get()
        assert "已识别 7 个源文件" in app.scope_var.get()
        assert app.preview_id is None
        assert calls == ["scan"]
    finally:
        root.destroy()


def test_destroyed_app_releases_tk_variables_on_main_thread():
    root = tk.Tk()
    app = ConverterApp(root)
    finalized_on = []
    variable = app.source_var
    weakref.finalize(variable, lambda: finalized_on.append(threading.current_thread().name))
    app_ref = weakref.ref(app)
    variable_ref = weakref.ref(variable)

    root.destroy()
    del variable, app, root
    gc.collect()

    assert app_ref() is None
    assert variable_ref() is None
    assert finalized_on == ["MainThread"]


def test_destroyed_app_immediately_releases_app_and_panel_variables():
    root = tk.Tk()
    app = ConverterApp(root)
    finalized_on = []
    app_ref = weakref.ref(app)
    source_ref = weakref.ref(app.source_var)
    elapsed = app.progress_panel.elapsed_var
    elapsed_ref = weakref.ref(elapsed)
    weakref.finalize(elapsed, lambda: finalized_on.append(threading.current_thread().name))

    root.destroy()
    del elapsed, app, root

    assert app_ref() is None
    assert source_ref() is None
    assert elapsed_ref() is None
    assert finalized_on == ["MainThread"]


def test_destroyed_standalone_panel_immediately_releases_variables():
    root = tk.Tk()
    panel = ProgressPanel(root)
    elapsed_ref = weakref.ref(panel.elapsed_var)
    root.destroy()
    del panel, root
    assert elapsed_ref() is None


def test_root_destroy_releases_app_variables_even_while_app_is_referenced():
    root = tk.Tk()
    app = ConverterApp(root)
    source_ref = weakref.ref(app.source_var)
    status_ref = weakref.ref(app.status_var)
    root.destroy()
    assert source_ref() is None
    assert status_ref() is None


def test_finished_worker_app_releases_tk_variables_without_gc(tmp_path):
    def runner(config, action, progress, cancel):
        return {"action": action, "status": "complete", "counts": {}, "issues": [], "paths": {}}

    root = tk.Tk()
    app = ConverterApp(root, runner=runner)
    app.source_var.set(str(tmp_path))
    app.output_var.set(str(tmp_path / "out"))
    app.start_job("scan")
    pump(root, lambda: app.worker is None)
    app_ref = weakref.ref(app)
    root_ref = weakref.ref(root)
    panel_ref = weakref.ref(app.progress_panel)
    source_ref = weakref.ref(app.source_var)
    elapsed_ref = weakref.ref(app.progress_panel.elapsed_var)
    root.destroy()
    del app, root

    assert app_ref() is None
    assert root_ref() is None
    assert panel_ref() is None
    assert source_ref() is None
    assert elapsed_ref() is None


def test_cancelled_worker_app_releases_tk_variables_without_gc(tmp_path):
    def runner(config, action, progress, cancel):
        while not cancel():
            time.sleep(0.01)
        return {"action": action, "status": "cancelled", "counts": {}, "issues": [], "paths": {}}

    root = tk.Tk()
    app = ConverterApp(root, runner=runner)
    app.source_var.set(str(tmp_path))
    app.output_var.set(str(tmp_path / "out"))
    source_ref = weakref.ref(app.source_var)
    app.start_job("convert")
    pump(root, lambda: app.worker.is_alive())
    app.close()
    pump(root, lambda: app.worker is None)
    app_ref = weakref.ref(app)
    root_ref = weakref.ref(root)
    panel_ref = weakref.ref(app.progress_panel)
    del app, root

    assert app_ref() is None
    assert root_ref() is None
    assert panel_ref() is None
    assert source_ref() is None
