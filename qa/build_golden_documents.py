from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "golden_documents"
MANIFEST = ROOT / "golden_manifest.json"


def _html(title: str, date: str, source: str, paragraphs: list[str], *, navigation: str = "") -> str:
    date_meta = f'<meta name="pubdate" content="{date}">' if date else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title><meta name="ArticleTitle" content="{title}">{date_meta}
<meta name="ContentSource" content="{source}"></head><body>{navigation}
<article><h1>{title}</h1>{''.join(f'<p>{paragraph}</p>' for paragraph in paragraphs)}</article>
<footer>本地黄金测试夹具，不代表任何真实港口运行信息。</footer></body></html>"""


def build() -> list[dict[str, object]]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []

    def add(name: str, title: str, date: str, source: str, category: str, paragraphs: list[str],
            *, report_blocked: bool = False, duplicate_group: str = "", canonical_group: str = "",
            navigation: str = "") -> None:
        path = OUTPUT / f"{name}.html"
        path.write_text(_html(title, date, source, paragraphs, navigation=navigation), encoding="utf-8")
        records.append({
            "file": path.name, "title": title, "date": date, "source": source, "category": category,
            "report_blocked": report_blocked, "duplicate_group": duplicate_group,
            "canonical_group": canonical_group or name,
        })

    categories = [
        ("航行警告", "测试航行警告", "相关测试水域发布临时航行提示，船舶应核对公开原文和有效时段。"),
        ("海上气象", "测试海洋天气预警", "公开预报提示测试海域可能出现大风浪，相关单位应持续查看正式预报。"),
        ("港口作业", "测试港口作业安排", "公开信息说明测试作业窗口有所调整，具体安排以现场正式通知为准。"),
        ("航线运价", "测试航运指数变化", "公开指数材料显示测试航线数据发生变化，不构成价格预测或交易建议。"),
        ("政策监管", "测试交通监管通知", "公开政策材料说明测试申报流程发生调整，企业应核对正式条款。"),
        ("招标采购", "测试数字化采购公告", "公开采购栏目发布测试数字化项目需求，供应商应核对资格条件。"),
        ("企业动态", "测试港航企业动态", "公开新闻介绍测试合作项目进展，不代表任何内部经营判断。"),
    ]
    for index in range(12):
        category, prefix, fact = categories[index % len(categories)]
        add(
            f"normal_{index + 1:02d}", f"{prefix}{index + 1}", f"2026-07-{index + 1:02d}",
            "黄金测试公开机构", category,
            [fact, f"该条编号为{index + 1}，仅用于本地提取与分类质量评估，所有事实均为虚构测试，不得用于业务判断。"],
        )

    for index in range(4):
        category, prefix, fact = categories[(index + 4) % len(categories)]
        add(
            f"government_{index + 1:02d}", f"关于开展{prefix}工作的公开通知",
            f"2026-07-{13 + index:02d}", "黄金测试政府部门", category,
            [f"为验证政府公告结构，现发布{fact}", "有关单位应以原始正式文件为准，本夹具不包含真实行政要求。"],
        )

    nav = "<nav>" + "".join(
        "<a>首页</a><a>新闻资讯</a><a>集团概况</a><a>业务服务</a><a>联系我们</a>"
        for _ in range(4)
    ) + "</nav>"
    for index in range(4):
        add(
            f"noise_{index + 1:02d}", "网站首页" if index == 0 else "黄金测试网站",
            f"2026-07-{17 + index:02d}", "黄金测试网站", "企业动态",
            ["首页 新闻资讯 集团概况 业务服务 联系我们 " * 4],
            report_blocked=True, navigation=nav,
        )

    broken = "å±±ä¸æ¸¯å£å¬å¼ä¿¡æ¯ Ã¥Â±Â± Ã¦Â¸Â¯ � �"
    for index in range(4):
        add(
            f"mojibake_{index + 1:02d}", broken, f"2026-07-{21 + index:02d}",
            "黄金测试公开机构", "企业动态", [broken * 5, broken * 4], report_blocked=True,
        )

    for index in range(2):
        add(
            f"no_date_{index + 1:02d}", f"无日期公开测试通知{index + 1}", "",
            "黄金测试公开机构", "政策监管",
            ["本页故意不提供发布日期，用于验证日期缺失门禁。", "正文结构完整，但不得直接进入正式报告。"],
            report_blocked=True,
        )

    duplicate_text = [
        "公开测试信息提示相关水域临时限航，具体时间和范围必须回到原文核对。",
        "该内容用于验证URL不同但正文相同的重复识别，不代表真实航行状态。",
    ]
    for index in range(2):
        add(
            f"duplicate_{index + 1:02d}", "重复航行提示测试", "2026-07-25",
            "黄金测试海事机构", "航行警告", duplicate_text, duplicate_group="DUP-1",
        )

    add(
        "updated_01", "测试作业安排更新前", "2026-07-26", "黄金测试港口机构", "港口作业",
        ["公开测试作业窗口原计划在上午进行，具体安排需核对原文。", "这是同一页面旧版本，用于版本关联评估。"],
        canonical_group="UPDATE-1",
    )
    add(
        "updated_02", "测试作业安排更新后", "2026-07-26", "黄金测试港口机构", "港口作业",
        ["公开测试作业窗口调整为下午进行，具体安排需核对原文。", "这是同一页面新版本，用于版本关联评估。"],
        canonical_group="UPDATE-1",
    )

    for index in range(2):
        add(
            f"resolved_{index + 1:02d}", f"解除测试航行风险预警{index + 1}", f"2026-07-{27 + index:02d}",
            "黄金测试海事机构", "航行警告",
            [f"公开测试信息说明第{index + 1}项风险提示已经解除，原事件仍应保留历史记录。", "解除状态必须由人工关联原事件后才能进入报告。"],
        )

    MANIFEST.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return records


if __name__ == "__main__":
    print(json.dumps({"fixture_count": len(build()), "output": str(OUTPUT)}, ensure_ascii=False))
