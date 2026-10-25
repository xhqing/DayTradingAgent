#!/usr/bin/env python3
"""复盘报告演化图嵌入校验（2026-10-24 用户立：「7 张序贯演化图 需要放在复盘报告里面」）。

为什么需要工具：`review-and-evaluation.md` 早已写明「演化图是复盘报告的必含产物（嵌入报告）」，
但 2026-10-24 美股全量复盘仍只在「配套产物」表里列了 png 文件名、没在正文里嵌图——散文规定会衰减，
故做机械校验。

用法：
  python3 check_review_images.py reviews/2026-10-24-review-us.md
  python3 check_review_images.py reviews/2026-10-24-review-us.md --suffix -us   # 显式指定后缀
  python3 check_review_images.py --all reviews/

判定：报告正文必须用 Markdown 图片语法 `![说明](文件名.png)` 引用**全部 7 张**演化图
（bayes / ev / winrate / pg / psum40 / pg20 / psum40-20pct），且图片文件真实存在。
「配套产物」表里只写文件名不算数（那是清单、不是嵌入）。

退出码：0 全部通过；1 有缺失。
"""
import argparse
import glob
import os
import re
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "..", "..", "..", ".."))

# 7 张演化图的后缀（顺序即报告建议顺序）
FIGS = [
    ("bayes", "贝叶斯 P(EV>0) 演化"),
    ("ev", "累计 EV 演化"),
    ("winrate", "贝叶斯胜率演化"),
    ("pg", "P(g>0) 演化（多 f）"),
    ("psum40", "P(∑40≥0) 演化（多 f）"),
    ("pg20", "P(g≥ln1.2/40) 演化（多 f）"),
    ("psum40-20pct", "P(∑40≥ln1.2) 演化（多 f）"),
]


def parse_report(path):
    """从报告路径解析 (date, suffix)。reviews/2026-10-24-review-us.md → ('2026-10-24', '-us')。"""
    base = os.path.basename(path)
    m = re.match(r"(\d{4}-\d{2}-\d{2})-(?:review|HKT-review|ET-review|review-hk|review-us)(?:-(hk|us))?",
                 base)
    if not m:
        m2 = re.search(r"(\d{4}-\d{2}-\d{2})", base)
        return (m2.group(1) if m2 else None), ""
    return m.group(1), ("-" + m.group(2) if m.group(2) else "")


def check(path, suffix=None):
    date, auto_suffix = parse_report(path)
    if not date:
        print(f"❌ 无法从文件名解析复盘日期：{path}")
        return False
    sfx = suffix if suffix is not None else auto_suffix
    reviews_dir = os.path.dirname(os.path.abspath(path))

    try:
        txt = open(path, encoding="utf-8").read()
    except OSError as e:
        print(f"❌ 读不到报告：{e}")
        return False

    # 收集报告里真实嵌入的图片（Markdown 语法）
    embedded = re.findall(r"!\[[^\]]*\]\(([^)]+\.png)\)", txt)
    embedded_names = {os.path.basename(p) for p in embedded}

    print(f"=== 校验 {os.path.basename(path)}（date={date}, suffix='{sfx or '无'}') ===")
    ok = True
    for key, label in FIGS:
        # 文件名兼容三种命名：{date}{sfx}-{key}（新）/ {date}-{key}（无后缀，历史）/ {date}-hk|us-{key}
        cands = [f"{date}{sfx}-{key}-evolution.png"]
        for alt in ("", "-hk", "-us", "-HKT", "-ET"):
            cands.append(f"{date}{alt}-{key}-evolution.png")
        hit_img = next((c for c in cands if c in embedded_names), None)
        hit_file = next((c for c in cands if os.path.exists(os.path.join(reviews_dir, c))), None)
        if hit_img and hit_file:
            print(f"✅ {label}：已嵌入 {hit_img}")
        elif hit_img and not hit_file:
            print(f"❌ {label}：报告嵌入了 {hit_img}，但文件不存在（先跑 bayes_evolution.py）")
            ok = False
        elif hit_file and not hit_img:
            print(f"❌ {label}：报告未嵌入 {hit_file}（需 `![{label}]({hit_file})`）")
            ok = False
        else:
            print(f"❌ {label}：图片文件与嵌入引用都缺失（{cands[0]}）")
            ok = False
    if ok:
        print(f"\n✅ 全部 7 张演化图已嵌入报告正文（共 {len(embedded_names)} 张引用）")
    else:
        print("\n⚠️ 报告缺图：按「7 张序贯演化图 需要放在复盘报告里面」补齐后再交付"
              "（只在配套产物表列文件名不算嵌入）")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?", help="复盘报告 md 路径")
    ap.add_argument("--suffix", default=None, help="市场后缀，如 -hk / -us（缺省从文件名推断）")
    ap.add_argument("--all", metavar="DIR", help="批量校验目录下所有复盘报告")
    a = ap.parse_args()

    if a.all:
        paths = sorted(glob.glob(os.path.join(a.all, "*review*.md")))
        if not paths:
            print(f"目录下无复盘报告：{a.all}")
            return 1
        bad = [p for p in paths if not check(p)]
        print(f"\n=== 汇总：{len(paths)} 份报告，{len(bad)} 份缺图 ===")
        for p in bad:
            print(f"  ❌ {os.path.basename(p)}")
        return 0 if not bad else 1

    if not a.report:
        ap.error("需要报告路径，或用 --all <dir>")
    return 0 if check(a.report, a.suffix) else 1


if __name__ == "__main__":
    sys.exit(main())
