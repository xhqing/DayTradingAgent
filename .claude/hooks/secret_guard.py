#!/usr/bin/env python3
"""凭证读取守卫 hook（2026-08-17 立，PreToolUse Bash）。

为什么：老虎实盘与模拟盘共用同一套 API 凭证（tiger_id + RSA 私钥 + license，存
~/.tigeropen/ 的 properties 与项目 accounts.json），实盘 7 位账户号在 accounts.json。
这些文件对 AI 没有秘密可言——AI 用 cat / grep / python open 都能把私钥与账户号读进
对话上下文，进而随上下文进入模型请求。permissions.deny 只能挡 Read 工具，本 hook 在
Bash 关卡补挡常见的命令行读取路径（cat / grep / less / head / tail / base64 / strings /
python open 等 + 凭证路径特征）。

拦什么（两个条件同时满足才拦）：
1. 命令文本里出现凭证路径特征：.tigeropen / tiger_openapi_config / accounts.json /
   private_key / TIGEROPEN_PRIVATE_KEY；
2. 且伴随读取动作特征：cat / grep / less / more / head / tail / sed -n / awk / base64 /
   strings / xxd / od / python 里 open( 或 .read_text( 或 print 整个 config 对象。

放什么（不误伤正常交易链路）：
- 只写不读的操作（chmod / mv / ls / stat / diff -q）不拦——不含读取特征；
- 运行 trade 脚本本身（open_position_tiger.py 等）不拦——脚本内部读凭证属正常业务，
  拦了 auto 模式就没法下单了；hook 只拦「AI 在命令行层直接读凭证内容」；
- git check-ignore / git status 等只读 git 元数据不涉及文件内容的命令不拦。

局限（诚实，与 monitor_guard 同口径）：正则特征覆盖常见路径而非全部——AI 理论上可用
变量拼接、间接引用等绕过。本 hook 是抬高成本 + 暴露行为，不是 100% 银弹；真正的硬隔离
须走平台层（独立 API key + IP 白名单 + 实盘 key 不落 AI 可达路径，见 TODO）。

用法（hooks 注册，2026-09-02 起双宿主：CC settings.json + ZCode .zcode/config.json 同挂）：
  PreToolUse matcher Bash → python3 .claude/hooks/secret_guard.py pretool
hook 接收 stdin JSON（tool_name / tool_input，两宿主字段同名），exit 0 放行 / exit 2 阻断 + stderr 提醒
（阻断语义两宿主一致：stderr 均作为拦截原因反馈 AI）。
"""
import sys

# 凭证路径特征（命中其一即视为「碰凭证文件」）
_SECRET_PATH_PATTERNS = [
    '.tigeropen',
    'tiger_openapi_config',
    'accounts.json',
    'private_key',
    'TIGEROPEN_PRIVATE_KEY',
]

# 读取动作特征（命中其一即视为「要读内容」）
_READ_ACTION_PATTERNS = [
    'cat ', 'cat<', '| cat', 'grep ', 'grep -', 'egrep ', 'fgrep ', 'rg ',
    'less ', 'more ', 'head ', 'tail ', 'sed -n', 'awk ',
    'base64 ', 'base64 -', '| base64', 'strings ', 'xxd ', 'od ',
    'open(', 'read_text(', 'read_bytes(', '.read()',
    'print(config', 'print(cfg', 'vars(config', 'config.__dict__',
    'jq ', 'jq .',
]

# 不读文件内容的操作（命令整体只含这些动词 + 凭证路径时放行：改权限 / 移动 / 看元数据）
_NON_READ_VERBS = ('chmod ', 'chown ', 'mv ', 'cp ', 'ls ', 'stat ', 'file ', 'touch ',
                   'rm ', 'ln ', 'diff ')


def _mentions_secret(cmd: str) -> bool:
    return any(p in cmd for p in _SECRET_PATH_PATTERNS)


def _has_read_action(cmd: str) -> bool:
    return any(p in cmd for p in _READ_ACTION_PATTERNS)


def _is_trade_script_run(cmd: str) -> bool:
    """运行 trade skill 自家脚本（正常业务链路，脚本内部读凭证不拦）。

    判定：命令里以脚本文件名方式引用 trade scripts 目录下的 .py / .sh
    （python3 .../open_position_tiger.py、bash .../alert.sh 等）。
    accounts.json / properties 本身不是脚本，不会命中。
    """
    for name in (
        'trade_utils_tiger.py', 'trade_utils_tiger_us.py',
        'open_position_tiger.py', 'open_position_tiger_us.py',
        'close_position_tiger.py', 'close_position_tiger_us.py',
        'move_stop_tiger.py', 'move_stop_tiger_us.py',
        'monitor_segment.py', 'monitor_summary.py', 'resume.py',
        'preflight.py', 'capital.py', 'ws_segment.py', 'futu_ws_segment.py',
        'bayes_evolution.py', 'review.py', 'hot_list.py', 'snapshot.py',
        'kline.py', 'monitor.py', 'classify_hk_security.py', 'fee_schedule.py',
        'alert.sh', 'ring.sh', 'log_signal.sh', 'log_action.sh',
        'monitor_register.sh', 'monitor_unregister.sh',
    ):
        if name in cmd and '.py' in cmd or name in cmd and '.sh' in cmd:
            # 还须确认整个命令不直接读凭证内容（脚本名撞上 cat accounts.json 的组合仍拦：
            # 脚本放行只针对「运行脚本」，命令里另有直接读取动作时按读取处理）
            return True
    return False


def main():
    import json
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)   # 解析失败放行（hook 不该因自身问题卡死交易流程）
    tool_input = data.get('tool_input') or {}
    cmd = str(tool_input.get('command', ''))
    if not cmd:
        sys.exit(0)
    if _mentions_secret(cmd) and _has_read_action(cmd):
        # 放行例外 1：运行 trade 自家脚本属正常业务链路（脚本内部读凭证不下发到命令行输出）。
        # 例外只覆盖「命令里没有独立的直接读取片段」——用 && / ; / | 切出子命令逐段判：
        # 每段要么是跑脚本、要么不含读取动作，才放行；任一段既非跑脚本又直接读凭证 → 拦。
        import re as _re
        segments = _re.split(r'&&|\|\||;|\|', cmd)
        # 例外 2：非读取类操作（chmod / ls / stat / diff 等只碰元数据 / 只写不读）放行——
        # 判定口径：对每个「既碰凭证路径又含读取特征」的段，若它同时含非读取动词且该动词
        # 紧跟凭证路径（如 chmod 600 <path>），视为元数据 / 写操作段；否则为直接读取段。
        direct_read = False
        # 「先搬走再读」间接路径：cp / mv 把凭证复制到普通路径、后续段读副本——
        # 外层已保证整条 cmd 是「凭证特征 + 读取特征」并存；只要命令里有 cp / mv 段
        # 碰凭证路径、且其它任一段有读取动作，按保守口径直接判读（纯 cp 无读取段
        # 不进本分支，外层 _has_read_action 已挡）。
        if any((s.strip().split(' ')[0:1] == ['cp'] or s.strip().split(' ')[0:1] == ['mv']) and _mentions_secret(s) for s in segments):
            if any(_has_read_action(s) for s in segments):
                direct_read = True
        for seg in segments:
            if not (_has_read_action(seg) and _mentions_secret(seg)):
                continue
            if _is_trade_script_run(seg):
                continue   # 跑脚本段不算直接读
            # 段内是否有「非读取动词直接作用于凭证路径」的形式（chmod/ls/stat 等开头用法）
            seg_meta = any(verb in seg for verb in _NON_READ_VERBS)
            # 读特征与凭证路径是否出现在同一 token 组（如 cat <path> 拦、chmod <path> 放）：
            # 简化口径——段首动词（去掉环境变量前缀后第一个词）是读取类才拦。
            first_word = _re.sub(r'^[A-Za-z_][A-Za-z0-9_]*=\S+\s+', '', seg).strip().split(' ')[0] if seg.strip() else ''
            read_verbs = {'cat', 'grep', 'egrep', 'fgrep', 'rg', 'less', 'more', 'head',
                          'tail', 'awk', 'base64', 'strings', 'xxd', 'od', 'jq', 'python',
                          'python3', 'sed'}
            if first_word in read_verbs:
                direct_read = True
                break
            if not seg_meta and any(p in seg for p in ('open(', 'read_text(', 'read_bytes(', '.read()', 'print(config', 'vars(config')):
                direct_read = True   # python -c / 内嵌代码读取特征（首词可能不是 python）
                break
            if first_word in ('cp', 'mv', 'chmod', 'chown', 'ls', 'stat', 'file', 'touch', 'rm', 'ln', 'diff'):
                # cp / mv 把凭证复制到别处再读的间接路径：若同命令其它段对副本路径有读取特征
                #（如 cp acc.json /tmp/x && cat /tmp/x），上面的分段循环里那段虽不含凭证路径、
                # 但整条 cmd 里凭据特征 + 读取特征并存——这类「先搬走再读」按保守口径拦截：
                # 副本目标路径出现在后续读取段中即拦。
                dest = seg.strip().split(' ')[-1] if ' ' in seg.strip() else ''
                if dest:
                    others = [s for s in segments if s is not seg]
                    if any(dest in s and _has_read_action(s) for s in others):
                        direct_read = True
                        break
                continue   # 纯写 / 元数据操作，放行
            # 其它形态（如管道中段 cat）：保守拦截
            if any(p in seg for p in ('| cat', '| grep', '| base64', '| strings', '| head', '| tail')):
                direct_read = True
                break
        if not direct_read:
            sys.exit(0)
        sys.stderr.write(
            "⛔ 凭证读取守卫：该命令试图读取券商凭证文件（~/.tigeropen/ properties / "
            "accounts.json / 私钥）。实盘与模拟共用一套凭证，私钥与账户号不得进入对话上下文。"
            "需要账户信息时用打码口径（如 67****91）；需要切实盘确认时请用户自己核对账户号。\n"
            "如确需人工检查凭证内容，请用户在终端自行查看，不要让 AI 读。\n")
        sys.exit(2)
    sys.exit(0)


if __name__ == '__main__':
    main()
