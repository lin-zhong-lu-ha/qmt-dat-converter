"""Human-readable companion to the engine's machine-readable report."""

from pathlib import Path


STATUS = {"complete": "完成", "partial": "部分完成", "failed": "失败", "cancelled": "已取消",
          "verified": "已验证", "unverified": "未验证", "mismatch": "不匹配", "matched": "已匹配"}
ACTION = {"scan": "扫描", "convert": "转换", "verify": "核验"}


def format_report(report: dict) -> str:
    scope = report["scope"]
    counts = report["counts"]
    verification = report["verification"]
    lines = [f"QMT DAT {ACTION.get(report['action'], report['action'])}报告",
             f"状态: {STATUS.get(report['status'], report['status'])}",
             f"运行编号: {report['run_id']}",
             f"耗时: {report['elapsed_seconds']:.3f} 秒", "",
             "请求范围",
             f"源目录: {scope['source']}", f"输出目录: {scope['output']}",
             f"周期: {', '.join(scope['periods'])}",
             f"代码: {', '.join(scope['codes']) if scope['codes'] else '全部可识别代码'}",
             f"日期: {scope['start'] or '不限'} 至 {scope['end'] or '不限'}",
             f"模式: {scope['mode']}", "",
             "计数（日期是发现的源文件/证券/周期日期，部分完成时不代表全部已发布）",
             f"已识别源文件: {counts['source_files']}",
             f"发现新增源日期: {counts['new_days']}",
             f"发现修订源日期: {counts['revised_days']}",
             f"发现复用源日期: {counts['reused_days']}",
             f"已写入分区: {counts['written_partitions']}"]
    if report["action"] == "verify":
        lines.append(f"核验模式内容比对分区: {counts['content_verified_partitions']}")
    external = verification["external_evidence"]
    external_text = STATUS.get(external["status"], external["status"])
    if external["status"] == "unverified":
        external_text += "；无外部对照"
    else:
        provenance = external.get("provenance")
        if provenance == "same-source":
            external_text += "；同源对照；非独立真值"
        elif provenance == "independent":
            external_text += "；独立来源（证据文件声明，未经认证）"
        else:
            external_text += "；来源未注明"
        if "matched" in external and "expected" in external:
            external_text += f"；{external['matched']}/{external['expected']} 条匹配"
        if external.get("scope") is not None:
            external_text += f"；证据范围 {len(external['scope'])} 项"
    lines += ["", "核验维度",
             f"转换一致性: {STATUS.get(verification['conversion_consistency']['status'], verification['conversion_consistency']['status'])}",
             f"覆盖范围: {STATUS.get(verification['coverage']['status'], verification['coverage']['status'])}",
             f"外部证据: {external_text}",
             f"字段映射: {STATUS.get(verification['field_profile']['status'], verification['field_profile']['status'])}",
             f"价格校验范围: {scope['start'] or '不限'} 至 {scope['end'] or '不限'}；范围外异常只警告", "",
             "排除与问题",
             f"排除项: {sum(item['category'] in {'excluded', 'malformed'} for item in report['issues'])}",
             f"警告: {sum(item['category'] == 'warning' for item in report['issues'])}",
             f"错误: {sum(item['category'] == 'error' for item in report['issues'])}"]
    if report["issues"]:
        for issue in report["issues"]:
            lines.append(f"[{issue['category']}] {issue['reason']} {issue.get('path', '')}".rstrip())
            if issue["reason"] in {"invalid-ohlc", "invalid-ohlc-outside-scope"}:
                impact = "范围内异常，阻止发布" if issue.get("blocking", True) else "范围外异常，不阻塞本次转换"
                lines.append(f"  {issue.get('code', '')} | {impact} | 异常记录 {issue.get('count', 0)} 条")
                for row in issue.get("rows", []):
                    lines.append(f"  {row['date']} | 开盘 {row['open']} | 最高 {row['high']} | 最低 {row['low']} | 收盘 {row['close']}")
                if issue.get("count", 0) > len(issue.get("rows", [])):
                    lines.append("  仅展示前10条，完整异常日期见JSON的days字段")
            if issue["reason"] == "partition-unpublishable":
                lines.append(f"  未更新分区: {issue.get('path', '')} | 阻塞股票: {', '.join(issue.get('blocking_codes', [])) or '详见前述错误'}")
    else:
        lines.append("无")
    lines += ["", f"JSON 报告: {report['paths']['report']}"]
    return "\n".join(lines) + "\n"


def write_text_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_report(report), encoding="utf-8")
