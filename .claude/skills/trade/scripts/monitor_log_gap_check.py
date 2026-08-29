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


def gap_overlaps_hk_lunch(t1, t2):
    """gap 是否覆盖港股午休时段（12:00-13:00）。

    2026-08-17 立：港股午间休市 60 分钟是制度性停盘、不是降频——上午末点 11:5x 到
    下午首点 13:0x 的 gap ≈ 60+ 分钟必超阈值，照标「降频疑点」是误报。判据：gap 的
    时间区间 [t1, t2] 与 [12:00, 13:00] 有交集（gap 起点在午休前、终点在午休开始之后）。
    t1/t2 为 HH:MM:SS 字符串；跨午夜场景（美股）不会踩到港股午休、按无交集处理。"""
    def secs(t):
        h, m, s = map(int, t.split(":"))
        return h * 3600 + m * 60 + s
    a1, a2 = secs(t1), secs(t2)
    if a2 < a1:          # 跨午夜（t2 在次日）：拆成 [t1,24:00) 与 [0,t2]，后者不涉港股午休
        a2 += 24 * 3600
    lunch_start, lunch_end = 12 * 3600, 13 * 3600
    return a1 < lunch_end and a2 > lunch_start


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
    lunch_skips = []   # 覆盖港股午休的 gap（制度性休市、不算疑点，单列展示）
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
            if gap_overlaps_hk_lunch(t1, t2):
                # 港股午休（12:00-13:00）制度性停盘，gap 大是正常休市、不是降频（2026-08-17 立）
                lunch_skips.append((i, t1, t2, g))
                continue
            suspects.append((i, t1, t2, g))

    print(f"{path}: {len(rows)} 点 | gap 阈值 {threshold}s")
    if gaps:
        avg = sum(gaps) / len(gaps)
        mx = max(gaps)
        print(f"  gap 统计：平均 {avg:.0f}s / 最大 {mx}s")
    if lunch_skips:
        print(f"  🍚 {len(lunch_skips)} 处 gap 覆盖港股午休（12:00-13:00 制度性休市，不算降频）:")
        for i, t1, t2, g in lunch_skips:
            print(f"    第 {i} 点：{t1} → {t2}，gap {g}s（{g / 60:.1f} 分钟）")
    if suspects:
        print(f"  ⚠️ {len(suspects)} 处降频疑点（gap > {threshold}s，密采样正常段间 < 90s）：")
        for i, t1, t2, g in suspects:
            print(f"    第 {i} 点：{t1} → {t2}，gap {g}s（{g / 60:.1f} 分钟）")
    else:
        print(f"  ✅ 无降频疑点（所有相邻 gap ≤ {threshold}s）")


if __name__ == "__main__":
    main()
