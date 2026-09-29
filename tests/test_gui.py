import time
import tkinter as tk
import threading

import pytest

from qmt_dat_converter.model import ConverterError


def pump(root, condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("GUI worker did not reach expected state")


def destroyed(root):
    try:
        return not root.winfo_exists()
    except tk.TclError:
        return True


def test_form_normalization_and_real_scan_worker(tmp_path, dat_file, row):
    from qmt_dat_converter.gui import ConverterApp

    source = tmp_path / "安装" / "datadir"
    dat_file(source / "SH" / "86400" / "600000.DAT", row)
    root = tk.Tk()
    try:
        app = ConverterApp(root)
        app.source_var.set(str(source))
        app.output_var.set(str(tmp_path / "输出"))
        app.period_var.set("日线")
        app.codes_var.set("600000.sh， 000001.SZ")
        app.start_var.set("20260922")
        app.end_var.set("2026-09-22")
        config = app.config_from_form("range")
        assert config.periods == ("1d",)
        assert config.start == config.end == "2026-09-22"
        assert config.mode == "range"
        assert config.codes == ("600000.SH", "000001.SZ")
        app.output_var.set("")
        with pytest.raises(ConverterError, match="目录"):
            app.config_from_form("range")
        app.output_var.set(str(tmp_path / "输出"))
        app.start_job("scan")
        pump(root, lambda: app.worker is None)
        assert app.last_report["status"] == "complete"
        assert app.last_report["action"] == "scan"
        assert app.last_report["paths"]["text_report"]
        app.close()
        pump(root, lambda: destroyed(root))
    finally:
        if not destroyed(root):
            root.destroy()


def test_close_requests_cancellation_and_waits_for_worker(tmp_path):
    from qmt_dat_converter.gui import ConverterApp

    root = tk.Tk()
    release = False

    def slow_run(config, action="convert", progress=None, cancel=None):
        nonlocal release
        while not cancel():
            time.sleep(0.01)
        release = True
        return {"status": "cancelled", "action": action, "counts": {}, "issues": [],
                "paths": {"report": None, "text_report": None}}

    try:
        app = ConverterApp(root, runner=slow_run)
        app.source_var.set(str(tmp_path))
        app.output_var.set(str(tmp_path / "out"))
        app.start_job("convert")
        pump(root, lambda: app.worker is not None and app.worker.is_alive())
        app.close()
        assert root.winfo_exists()
        pump(root, lambda: destroyed(root))
        assert release
    finally:
        if not destroyed(root):
            root.destroy()


def test_scope_edit_during_scan_discards_stale_counts_and_runs_fresh_scan(tmp_path, dat_file, row):
    from qmt_dat_converter.engine import run
    from qmt_dat_converter.gui import ConverterApp

    source = tmp_path / "install" / "datadir"
    dat_file(source / "SH" / "86400" / "600000.DAT", row)
    dat_file(source / "SZ" / "86400" / "000001.DAT", row)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def held_first_scan(config, action="convert", progress=None, cancel=None):
        calls.append((action, config.codes))
        if len(calls) == 1:
            entered.set()
            assert release.wait(30)
        return run(config, action=action, progress=progress, cancel=cancel)

    root = tk.Tk()
    try:
        app = ConverterApp(root, runner=held_first_scan)
        app.source_var.set(str(source))
        app.output_var.set(str(tmp_path / "output"))
        app.period_var.set("日线")
        app.codes_var.set("600000.SH")
        app.start_job("scan")
        pump(root, entered.is_set)
        app.codes_var.set("600000.SH, 000001.SZ")
        pump(root, lambda: app.preview_id is None, timeout=3)
        assert app.worker is not None and app.worker.is_alive()
        release.set()
        pump(root, lambda: len(calls) >= 2 and app.worker is None)
        assert calls == [("scan", ("600000.SH",)),
                         ("scan", ("600000.SH", "000001.SZ"))]
        assert app.last_report["counts"]["source_files"] == 2
        assert "已识别 1 个源文件" not in app.scope_var.get()
        assert "已识别 2 个源文件" in app.scope_var.get()
    finally:
        release.set()
        if not destroyed(root):
            root.destroy()
