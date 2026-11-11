#!/usr/bin/env python3
"""会话标识的唯一来源（T185HKUS，2026-11-11 立）。

## 为什么需要

pi 注入的 `PI_SESSION_ID` 是 UUIDv7，前 8 位十六进制是毫秒时间戳的高 32 位，
每 65.536 秒才变一次。两个窗口相隔几十秒启动时，前 8 位完全相同——2026-11-10
美股盯盘实录：两个会话间隔 27 秒启动、前缀都是 `01a1c5e3`。于是所有「按前 8 位
区分会话」的标记、过滤和文件名都会串台：停盯总结把另一个窗口的 NVDA 两笔算进
本场，收尾标记与飞书去重标记也被两个会话共用。

## 统一口径

- **写入**（actions / signals 里的 `🆔 会话` 标记、按会话分文件的文件名、会话列）
  一律用 `session_tag()`：完整会话 id 去掉非字母数字字符（UUID 的连字符），得到
  32 位十六进制串。它仍是纯字母数字，既有正则 `([0-9a-zA-Z]+)` 能完整捕获。
- **读取**用 `session_matches()`：新格式精确相等才算本会话；改造前写入的 8 位短
  标记按前缀兼容（旧标记正好是新标记的前 8 位）。旧标记无法区分并发会话，调用方
  必须用 `is_legacy()` 判定并显式告警，不能静默把别人的记录算进来。
- **拿不到 `PI_SESSION_ID`**（人工终端直接跑脚本）时返回空串，沿用原来的「无会话
  后缀 / 无归属标记」回退，不改变既有行为。

改动前各脚本各自写 `PI_SESSION_ID[:8]`，口径散在十几个文件里；集中到这里后，
以后再要调整标识格式只改本文件一处。
"""

import os
import re

# 改造前的短标记长度（UUIDv7 时间戳高 32 位，正好 8 个十六进制字符）
LEGACY_LEN = 8


def session_tag(sid=None):
    """本会话的完整标识（32 位十六进制）；显式传 sid 时以传入值归一化后的结果为准。"""
    raw = sid if sid is not None else (os.environ.get("PI_SESSION_ID") or "")
    return re.sub(r"[^0-9A-Za-z]", "", str(raw).strip())


def is_legacy(tag):
    """是否为改造前的 8 位短标记（这类标记无法区分并发会话）。"""
    t = str(tag or "").strip()
    return bool(t) and len(t) <= LEGACY_LEN


def session_matches(tag, sid=None):
    """记录里的会话标记是否属于本会话。

    - 新格式（完整标识）：精确相等才算，别的会话一律不混入；
    - 旧格式（≤8 位）：本会话完整标识以它开头即算命中，仅用于兼容历史文件。
      这类命中是模糊的（两个并发会话可能共用同一前 8 位），调用方必须显式告警。
    """
    cur = session_tag(sid)
    t = str(tag or "").strip()
    if not cur or not t:
        return False
    if t == cur:
        return True
    return is_legacy(t) and cur.startswith(t)
