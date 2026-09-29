import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

import pytest

from qmt_dat_converter.model import ConverterError


PROJECT = Path(__file__).resolve().parents[1]


def invoke(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(PROJECT / "run_converter.py"), *map(str, args)],
        cwd=cwd or PROJECT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_missing_local_config_has_no_machine_defaults_and_does_not_create_files(tmp_path):
    from qmt_dat_converter.settings import load_settings

    path = tmp_path / "config.local.json"
    assert load_settings(path) == {"source": "", "output": "", "period": "both"}
    assert not path.exists()
    with pytest.raises(ConverterError, match="config"):
        load_settings(path, required=True)


def test_relative_paths_resolve_next_to_config_not_process_directory(tmp_path, monkeypatch):
    from qmt_dat_converter.settings import load_settings

    path = tmp_path / "电脑配置" / "config.local.json"
    path.parent.mkdir()
    path.write_text('{"source":"行情/datadir","output":"结果","period":"1d"}', encoding="utf-8-sig")
    monkeypatch.chdir(tmp_path)
    settings = load_settings(path)
    assert settings["source"] == str(path.parent / "行情/datadir")
    assert settings["output"] == str(path.parent / "结果")
    assert settings["period"] == "1d"


@pytest.mark.parametrize("content", [
    "{", "[]", '{"source":123}', '{"period":"5m"}', '{"source_dir":"wrong-key"}',
])
def test_invalid_settings_fail_explicitly_without_fallback(tmp_path, content):
    from qmt_dat_converter.settings import load_settings

    path = tmp_path / "config.local.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConverterError, match="config"):
        load_settings(path)
    assert path.read_text(encoding="utf-8") == content


def test_save_and_reload_local_paths_without_persisting_job_date_filters(tmp_path):
    from qmt_dat_converter.settings import load_settings, save_settings

    path = tmp_path / "config.local.json"
    save_settings(path, source="安装/datadir", output="输出", period="1m")
    assert load_settings(path) == {
        "source": str(tmp_path / "安装/datadir"), "output": str(tmp_path / "输出"), "period": "1m",
    }
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"source", "output", "period"}
    original = path.read_bytes()
    with pytest.raises(ConverterError):
        save_settings(path, source="", output="输出", period="both")
    assert path.read_bytes() == original


def test_cli_config_drives_real_conversion_from_other_directory(tmp_path, dat_file, row):
    source = tmp_path / "安装/datadir"
    dat_file(source / "SH/86400/600000.DAT", row)
    path = tmp_path / "config.local.json"
    path.write_text('{"source":"安装/datadir","output":"结果","period":"1d"}', encoding="utf-8")
    result = invoke("convert", "--config", path, cwd=tmp_path.parent)
    assert result.returncode == 0, result.stderr
    report = json.loads(Path(result.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8"))
    assert report["counts"]["source_files"] == 1
    assert report["counts"]["written_partitions"] == 1
    assert report["scope"]["periods"] == ["1d"]
    assert (tmp_path / "结果/data/qmt-daily-monthly/2026/09/SH.parquet").exists()


def test_cli_explicit_paths_override_config_without_touching_saved_targets(tmp_path, dat_file, row):
    source = tmp_path / "other/datadir"
    dat_file(source / "SZ/86400/000001.DAT", row)
    path = tmp_path / "config.local.json"
    path.write_text('{"source":"absent","output":"unused","period":"1m"}', encoding="utf-8")
    result = invoke("scan", "--config", path, "--source", source,
                    "--output", tmp_path / "chosen", "--period", "1d")
    assert result.returncode == 0, result.stderr
    report = json.loads(Path(result.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8"))
    assert report["counts"]["source_files"] == 1
    assert report["scope"]["periods"] == ["1d"]
    assert not (tmp_path / "unused").exists()


def test_unconfigured_cli_refuses_operation_and_explicit_missing_config_is_error(tmp_path):
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    result = invoke("scan", "--config", config)
    assert result.returncode != 0
    assert "source" in result.stderr and "output" in result.stderr
    missing = invoke("scan", "--config", tmp_path / "typo.json")
    assert missing.returncode != 0
    assert "config" in missing.stderr
    assert list(tmp_path.iterdir()) == [config]


def test_gui_saved_paths_are_loaded_after_restart(tmp_path):
    import tkinter as tk
    from qmt_dat_converter.gui import ConverterApp

    config = tmp_path / "config.local.json"
    root = tk.Tk()
    try:
        app = ConverterApp(root, config_path=config)
        assert app.source_var.get() == app.output_var.get() == ""
        app.source_var.set(str(tmp_path / "安装/datadir"))
        app.output_var.set(str(tmp_path / "输出"))
        app.period_var.set("日线")
        app.save_config()
        assert config.exists()
        app.close()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    root = tk.Tk()
    try:
        app = ConverterApp(root, config_path=config)
        assert app.source_var.get() == str(tmp_path / "安装/datadir")
        assert app.output_var.get() == str(tmp_path / "输出")
        assert app.period_var.get() == "日线"
        app.close()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_config_save_failure_preserves_previous_file(tmp_path, monkeypatch):
    from qmt_dat_converter import settings

    path = tmp_path / "config.local.json"
    settings.save_settings(path, source="source", output="output", period="1d")
    original = path.read_bytes()

    def fail_replace(*_):
        raise PermissionError("locked destination")

    monkeypatch.setattr(settings.os, "replace", fail_replace)
    with pytest.raises(ConverterError, match="config-save-failed"):
        settings.save_settings(path, source="changed", output="changed-output", period="1m")
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")
def test_windows_launcher_prefers_checkout_venv_from_other_directory(tmp_path):
    checkout = tmp_path / "portable-checkout"
    checkout.mkdir()
    venv = checkout / ".venv"
    created = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                             capture_output=True, text=True)
    assert created.returncode == 0, created.stderr
    shutil.copyfile(PROJECT / "start_converter.ps1", checkout / "start_converter.ps1")
    (checkout / "run_converter.py").write_text(
        "import json, sys\nprint(json.dumps({'prefix':sys.prefix,'args':sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(checkout / "start_converter.ps1"), "scan", "--period", "1d"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert Path(payload["prefix"]).resolve() == venv.resolve()
    assert payload["args"] == ["scan", "--period", "1d"]
