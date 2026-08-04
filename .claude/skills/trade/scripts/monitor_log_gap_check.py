#!/usr/bin/env python3
"""monitor_log 采样 gap 复盘检查（C8，2026-08-04 多层防护）。

遍历指定 monitor_log CSV，检查相邻采样时间戳 gap，标记 > 阈值的降频疑点段。
复盘用：暴露盯盘期间擅自降频（cron / 直接 snapshot 替代 monitor_segment）的段间大 gap
——正常密采样段间 gap ≈ 采样间隔（10 秒）+ 段时长（40 秒）≈ 50 秒；降频会留下 >> 阈值的 gap。

用法：
  python3 monitor_log_gap_check.py <monitor_log.csv> [gap_threshold_seconds]
  默认阈值 120 秒（2 分钟）——段间循环正常 < 90 秒，> 120 秒 = 疑似降频/断层。

示例：
  python3 monitor_log_gap_check.py tmp/monitor_log_HK_02359_20260804_signal.csv
"""
import csv
import sys


def gap_seconds(t1, t2):
    """算 t2 - t1 秒数（HH:MM:SS 格式，跨午夜 +24h）。"""
    h1, m1, s1 = map(int, t1.split(":"))
    h2, m2, s2 = map(int, t2.split(":"))
    diff = (h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1)
    if diff < 0:
        diff += 24 * 3600  # 跨午夜（美股夜盘）
    return diff


def main():
    if len(sys.argv) < 2:
        print("用法：python3 monitor_log_gap_check.py <monitor_log.csv> [gap_threshold_seconds=120]")
        sys.exit(1)
    path = sys.argv[1]
    threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 120

    with open(path) as f:
        rows = list(csv.DictReader(f))
    if len(rows) < 2:
        print(f"{path}: 采样点 {len(rows)} < 2，无法算 gap")
        return

    suspects = []
    gaps = []
    for i in range(1, len(rows)):
        t1 = rows[i - 1].get("time", "")
        t2 = rows[i].get("time", "")
        if not t1 or not t2:
            continue
        try:
            g = gap_seconds(t1, t2)
        except Exception:
            continue
        gaps.append(g)
        if g > threshold:
            suspects.append((i, t1, t2, g))

    print(f"{path}: {len(rows)} 点 | gap 阈值 {threshold}s")
    if gaps:
        avg = sum(gaps) / len(gaps)
        mx = max(gaps)
        print(f"  gap 统计：平均 {avg:.0f}s / 最大 {mx}s")
    if suspects:
        print(f"  ⚠️ {len(suspects)} 处降频疑点（gap > {threshold}s，密采样正常段间 < 90s）：")
        for i, t1, t2, g in suspects:
            print(f"    第 {i} 点：{t1} → {t2}，gap {g}s（{g / 60:.1f} 分钟）")
    else:
        print(f"  ✅ 无降频疑点（所有相邻 gap ≤ {threshold}s）")


if __name__ == "__main__":
    main()
