from decimal import Decimal


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def percentage(value):
    return "未知" if value is None else f"{Decimal(value) * 100:.1f}%"


def render_institutional(report):
    lines = ["# 机构配置与风格调研", ""]
    if report.get("data_kind") == "synthetic":
        lines.extend(["**合成数据：用于验收，不代表真实机构或投资信号。**", ""])
    lines.extend([
        f"报告季末：{cell(report['target_period'])}；研究截止：{cell(report['knowledge_cutoff'])}。",
        f"机构池：{cell(report['universe']['universe_id'])}，"
        f"{len(report['universe']['managers'])} 家；状态：{report['status']}。",
        "范围为样本公开披露的普通股／ADR；不能代表完整资产、净敞口或真实资金流。", "",
        "## 配置线索", "",
        "| 维度 | 分类 | 方向 | 可比机构 | 增／减／平 | 中位变化 bp | 连续区间 | 状态 |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ])
    selected = report["signals"][:5]
    for signal in selected:
        lines.append("| " + " | ".join(map(cell, [
            signal["dimension"], signal["label"], signal["direction"],
            f"{signal['eligible_count']}/{signal['expected_count']}",
            f"{signal['increase_count']}/{signal['decrease_count']}/{signal['unchanged_count']}",
            f"{signal['median_shift_bps']:.2f}", signal["consecutive_intervals"], signal["status"]])) + " |")
    if not selected:
        lines.extend(["", "当前数据不足以形成可核验的分类信号；保留已取得的机构持仓比较。"])
    lines.extend(["", "## 机构覆盖与变化", "",
                  "| 机构 CIK | 前期 → 本期 | 前期／本期价格覆盖 | 比较问题 |",
                  "|---|---|---|---|"])
    for comparison in report["comparisons"]:
        lines.append("| " + " | ".join(map(cell, [
            comparison["manager_cik"],
            f"{comparison['previous_period']} → {comparison['current_period']}",
            f"{percentage(comparison['coverage_previous'])} / {percentage(comparison['coverage_current'])}",
            ", ".join(comparison["issues"]) or "合格"])) + " |")
    lines.extend(["", "## 最新季度的证券变化", "",
                  "每机构展示最多五项，完整数量变化与来源在 JSON 中保留。", "",
                  "| 机构 CIK | 证券 | 披露变化 | 股数变化（公司行动见缺口） | 同价配置变化 bp |",
                  "|---|---|---|---:|---:|"])
    for comparison in report["comparisons"]:
        if comparison["current_period"] != report["target_period"]:
            continue
        ordered = sorted(comparison["changes"], key=lambda row: (
            -abs(row["allocation_shift"]) if row["allocation_shift"] is not None else Decimal(0),
            row["security_id"]))
        for change in ordered[:5]:
            shift = change["allocation_shift"]
            lines.append("| " + " | ".join(map(cell, [
                comparison["manager_cik"], change["security_id"], change["status"],
                change["delta_quantity"] if change["delta_quantity"] is not None else "未知",
                f"{shift * 10000:.2f}" if shift is not None else "未知"])) + " |")
    lines.extend(["", "## 后续研究", ""])
    for signal in selected:
        lines.extend([f"- {cell(signal['label'])}：核验经营、估值及资本配置能否解释该变化；"
                      "主动寻找反证。机构行为不代替公司研究。"])
    lines.extend(["", "## 数据缺口与来源", ""])
    for diagnostic in report["diagnostics"]:
        lines.append(f"- {cell(diagnostic.get('manager_cik', '样本'))} "
                     f"{cell(diagnostic.get('period', ''))}：{cell(diagnostic['code'])}")
    seen = set()
    for comparison in report["comparisons"]:
        for source in comparison["evidence"]:
            accession = source["accession"]
            if accession not in seen:
                seen.add(accession)
                lines.append(f"- {cell(accession)}；期间 {cell(source['report_period'])}；"
                             f"申报 {cell(source['filed'])}；{cell(source.get('source_url') or 'URL 未提供')}")
    lines.extend(["", "研究线索门槛仅用于选题，未验证预测收益。完整记录与未展开分类可用 --json 查看。", ""])
    return "\n".join(lines)
