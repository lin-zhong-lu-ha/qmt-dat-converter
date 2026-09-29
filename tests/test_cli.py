import json
import shutil
import subprocess
import sys
from pathlib import Path

import pyarrow.parquet as pq


PROJECT = Path(__file__).resolve().parents[1]


def invoke(*args):
    return subprocess.run([sys.executable, str(PROJECT / "run_converter.py"), *map(str, args)],
                          cwd=PROJECT, capture_output=True, text=True)


def test_help_and_invalid_dates():
    help_result = invoke("--help")
    assert help_result.returncode == 0
    assert "scan" in help_result.stdout
    invalid = invoke("scan", "--start", "2026-99-99")
    assert invalid.returncode != 0
    assert "invalid-date" in invalid.stderr
    missing_bound = invoke("convert", "--mode", "range", "--start", "20260922")
    assert missing_bound.returncode != 0
    assert "range" in missing_bound.stderr


def test_scan_convert_verify_subprocess_and_text_report(tmp_path, dat_file, row):
    source = tmp_path / "install" / "datadir"
    dat_file(source / "SH" / "86400" / "600000.DAT", row)
    output = tmp_path / "output"
    common = ("--source", source, "--output", output, "--period", "1d")

    scanned = invoke("scan", *common)
    assert scanned.returncode == 0, scanned.stderr
    assert "scan" in scanned.stdout
    assert not list((output / "data").rglob("*.parquet"))

    converted = invoke("convert", *common)
    assert converted.returncode == 0, converted.stderr
    report_path = Path(converted.stdout.strip().splitlines()[-1])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["scope"]["periods"] == ["1d"]
    assert report["scope"]["source"] == str(source)
    assert report["elapsed_seconds"] >= 0
    assert report["counts"]["written_partitions"] == 1
    text_report = Path(report["paths"]["text_report"])
    summary = text_report.read_text(encoding="utf-8")
    assert "发现新增源日期" in summary and "已写入分区: 1" in summary
    assert "排除项:" in summary and "错误:" in summary
    assert "核验模式内容比对分区" not in summary
    assert "覆盖范围" in summary and "未验证" in summary
    assert pq.read_table(output / "data/qmt-daily-monthly/2026/09/SH.parquet").num_rows == 1

    verified = invoke("verify", *common)
    assert verified.returncode == 0, verified.stderr
    verify_report = json.loads(Path(verified.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8"))
    assert verify_report["status"] == "complete"
    assert "核验模式内容比对分区: 1" in Path(verify_report["paths"]["text_report"]).read_text(encoding="utf-8")


def test_partial_and_failed_runs_exit_nonzero(tmp_path, dat_file, row):
    source = tmp_path / "install" / "datadir"
    bad = dat_file(source / "SH" / "86400" / "600000.DAT", row)
    output = tmp_path / "output"
    common = ("--source", source, "--output", output, "--period", "1d")
    assert invoke("convert", *common).returncode == 0
    bad.write_bytes(b"bad")
    partial = invoke("convert", *common)
    assert partial.returncode != 0
    assert "partial" in partial.stdout
    failed = invoke("scan", "--source", tmp_path / "missing", "--output", tmp_path / "failed")
    assert failed.returncode != 0
    assert "failed" in failed.stdout


def test_windows_launchers_accept_non_ascii_source(tmp_path, dat_file, row):
    if not shutil.which("powershell.exe"):
        return
    source = tmp_path / "样本安装" / "datadir"
    dat_file(source / "SH" / "86400" / "600000.DAT", row)
    for launcher in (["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                      str(PROJECT / "start_converter.ps1")],
                     ["cmd.exe", "/c", str(PROJECT / "启动转换工具.cmd")]):
        help_result = subprocess.run([*launcher, "--help"], cwd=PROJECT, capture_output=True, text=True)
        assert help_result.returncode == 0, help_result.stderr
        result = subprocess.run([*launcher, "scan", "--source", str(source), "--output",
                                 str(tmp_path / ("输出" + str(len(launcher)))), "--period", "1d"],
                                cwd=PROJECT, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "scan: complete" in result.stdout
