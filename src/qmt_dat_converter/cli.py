"""Command-line entry point for the offline converter."""

import argparse
from pathlib import Path
import sys

from .engine import run
from .model import Config, ConverterError, normalize_date
from .settings import DEFAULT_CONFIG, load_settings


def _parser():
    parser = argparse.ArgumentParser(description="QMT DAT 离线转换工具")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("scan", "convert", "verify"):
        command = actions.add_parser(name, help={"scan": "预览可识别文件", "convert": "转换数据", "verify": "核验输出"}[name])
        command.add_argument("--config", type=Path, help="本机 JSON 配置；默认读取工具目录的 config.local.json")
        command.add_argument("--source", type=Path, help="覆盖配置中的源目录")
        command.add_argument("--output", type=Path, help="覆盖配置中的输出目录")
        command.add_argument("--period", choices=("1d", "1m", "both"), help="覆盖配置中的周期")
        command.add_argument("--start", help="YYYYMMDD 或 YYYY-MM-DD")
        command.add_argument("--end", help="YYYYMMDD 或 YYYY-MM-DD")
        command.add_argument("--code", action="append", default=[], help="带交易所后缀的证券代码；可重复")
        command.add_argument("--mode", choices=("incremental", "full", "range"), default="incremental")
        command.add_argument("--evidence", type=Path, help="可选的离线 JSON 对照证据")
    return parser


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.mode == "range" and not (args.start and args.end):
        parser.error("range requires --start and --end")
    try:
        normalize_date(args.start)
        normalize_date(args.end)
        settings = load_settings(args.config or DEFAULT_CONFIG, required=args.config is not None)
        source = args.source if args.source is not None else settings["source"]
        output = args.output if args.output is not None else settings["output"]
        period = args.period or settings["period"]
        if not source or not output:
            raise ConverterError("config-required: set source and output in config.local.json or use --source and --output")
        config = Config(source, output,
                        periods=("1d", "1m") if period == "both" else (period,),
                        start=args.start, end=args.end, mode=args.mode,
                        codes=tuple(args.code), evidence=args.evidence)
    except ConverterError as exc:
        parser.error(str(exc))
    report = run(config, action=args.action)
    print(f"{args.action}: {report['status']}")
    for issue in report["issues"]:
        if issue["category"] == "error":
            print(f"{issue['reason']}: {issue.get('path', '')}", file=sys.stderr)
    if report["paths"]["report"]:
        print(report["paths"]["report"])
    return 0 if report["status"] == "complete" else 1
