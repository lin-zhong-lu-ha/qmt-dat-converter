import hashlib
import os

import pytest

from qmt_dat_converter.model import Config, ConverterError
from qmt_dat_converter.source import read_snapshot, scan_sources


def test_scan_selected_periods_and_exact_codes_reports_other_files(tmp_path, dat_file, row):
    root = tmp_path / "install" / "datadir"
    output = tmp_path / "output"
    dat_file(root / "SH" / "86400" / "600000.DAT", row)
    dat_file(root / "SH" / "60" / "600000.DAT", row)
    dat_file(root / "SZ" / "86400" / "399006.DAT", row)
    dat_file(root / "SH" / "86400" / "987654.DAT", row)
    dat_file(root / "SH" / "86400" / "odd.DAT", row)
    sources, issues = scan_sources(Config(root, output, periods=("1d",), codes=("600000.SH", "399006.SZ")))
    assert [(s.code, s.period, s.kind) for s in sources] == [
        ("600000.SH", "1d", "stock"), ("399006.SZ", "1d", "index")]
    assert any(issue["reason"] == "nonstandard-name" for issue in issues)
    assert any(issue["reason"] == "unclassified" for issue in issues)


def test_snapshot_contains_hash_and_stable_signature(tmp_path, dat_file, row):
    path = dat_file(tmp_path / "600000.DAT", row)
    result = read_snapshot(path)
    assert result.raw == path.read_bytes()
    assert result.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.signature["size"] == len(result.raw)


def test_snapshot_rejects_file_replaced_after_inventory(tmp_path, dat_file, row):
    root = tmp_path / "install" / "datadir"
    path = dat_file(root / "SH" / "86400" / "600000.DAT", row)
    sources, _ = scan_sources(Config(root, tmp_path / "output", periods=("1d",)))
    assert len(sources) == 1
    assert read_snapshot(path, source_root=root, expected_signature=sources[0].signature).raw == path.read_bytes()

    replacement = dat_file(tmp_path / "outside" / "replacement.DAT", row)
    os.replace(replacement, path)
    with pytest.raises(ConverterError, match="source-changed"):
        read_snapshot(path, source_root=root, expected_signature=sources[0].signature)


def test_snapshot_rejects_outside_symlink_after_inventory(tmp_path, dat_file, row):
    root = tmp_path / "install" / "datadir"
    path = dat_file(root / "SH" / "86400" / "600000.DAT", row)
    sources, _ = scan_sources(Config(root, tmp_path / "output", periods=("1d",)))
    outside = dat_file(tmp_path / "outside" / "600000.DAT", row)
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks unavailable on this host")
    with pytest.raises(ConverterError, match="source-"):
        read_snapshot(path, source_root=root, expected_signature=sources[0].signature)


def test_scan_rejects_outside_period_directory_link(tmp_path, dat_file, row):
    root = tmp_path / "install" / "datadir"
    outside = tmp_path / "outside"
    dat_file(outside / "600000.DAT", row)
    (root / "SH").mkdir(parents=True)
    try:
        (root / "SH" / "86400").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this host")
    sources, issues = scan_sources(Config(root, tmp_path / "output", periods=("1d",)))
    assert not sources
    assert any(issue["path"].endswith("86400") and issue["reason"] == "outside-source-directory" for issue in issues)


def test_snapshot_retries_when_file_changes_during_read(tmp_path, dat_file, row, monkeypatch):
    path = dat_file(tmp_path / "600000.DAT", row)
    original = type(path).read_bytes
    calls = 0

    def changing_read(self):
        nonlocal calls
        data = original(self)
        calls += 1
        if calls == 1:
            self.write_bytes(data + bytes(64))
        return data

    monkeypatch.setattr(type(path), "read_bytes", changing_read)
    result = read_snapshot(path, retries=2)
    assert calls == 2
    assert len(result.raw) == 8 + 2 * 64


def test_snapshot_rejects_persistent_mutation(tmp_path, dat_file, row, monkeypatch):
    path = dat_file(tmp_path / "600000.DAT", row)
    original = type(path).read_bytes

    def changing_read(self):
        data = original(self)
        self.write_bytes(data + bytes(64))
        return data

    monkeypatch.setattr(type(path), "read_bytes", changing_read)
    with pytest.raises(ConverterError, match="source-changed"):
        read_snapshot(path, retries=1)


@pytest.mark.parametrize("output_part", ["source", "source/child", "install", "install/results"])
def test_output_overlaps_source_or_installation(tmp_path, output_part):
    source = tmp_path / "install" / "source"
    source.mkdir(parents=True)
    output = tmp_path / output_part if output_part.startswith("install") else tmp_path / "install" / output_part
    with pytest.raises(ConverterError, match="output-path"):
        scan_sources(Config(source, output))


def test_source_inside_output_is_rejected(tmp_path):
    source = tmp_path / "install" / "datadir"
    source.mkdir(parents=True)
    with pytest.raises(ConverterError, match="output-path"):
        scan_sources(Config(source, tmp_path))


def test_missing_source_fails_closed(tmp_path):
    with pytest.raises(ConverterError, match="source"):
        scan_sources(Config(tmp_path / "missing", tmp_path / "output"))


def test_config_normalizes_dates_and_rejects_reverse_range(tmp_path):
    config = Config(tmp_path / "install" / "datadir", tmp_path / "output", start="20260922", end="2026-09-23")
    assert (config.start, config.end) == ("2026-09-22", "2026-09-23")
    with pytest.raises(ConverterError, match="date-range"):
        Config(config.source, config.output, start="2026-09-23", end="2026-09-22")
