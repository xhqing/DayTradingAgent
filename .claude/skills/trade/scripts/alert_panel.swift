// alert_panel.swift — 可最小化到 Dock 的提醒面板（DayTradingAgent T128，2026-09-01 立）
//
// 为什么需要：osascript `display dialog` 是模态窗——没有最小化按钮、一直悬浮在所有
// 窗口最上层（2026-08-31 用户实测反馈「一直悬在窗口最上层影响操作」）。本面板是真正的
// AppKit 窗口：带最小化（黄色）按钮、可收进 Dock、点 Dock 图标唤回，不遮挡其它操作。
//
// 用法（由 proxy_guard.py / monitor_watcher.py 调起，一般不手跑）：
//   alert_panel <pending_json> <result_json> [--timeout 秒]
//     pending_json 字段：
//       title           窗口标题（默认 "DayTradingAgent"）
//       message         正文（多行，支持 \n）
//       button_ok       确认按钮文案（默认 "已添加"）
//       button_cancel   取消按钮文案（默认 "取消"；设 "none" = 单按钮模式，如"知道了"）
//       timeout_secs    自动关闭秒数（默认 3600；超时按 timeout 写结果，由调用方冷却重弹）
//     点击后写 result_json：{"clicked": "ok" | "cancel" | "timeout"}
//   alert_panel --self-test
//     3 秒自动关的样例窗（编译产物冒烟用；结果写 stdout）
//
// 编译（proxy_guard 首次使用时自动做，产物 tmp/alert_panel 已 gitignore）：
//   swiftc -O -o <tmp>/alert_panel alert_panel.swift

import AppKit
import Foundation

func writeResult(_ path: String, _ clicked: String) {
    let s = "{\"clicked\": \"\(clicked)\"}"
    try? s.write(toFile: path, atomically: true, encoding: .utf8)
}

class PanelDelegate: NSObject, NSWindowDelegate {
    let resultPath: String
    var done = false
    init(resultPath: String) { self.resultPath = resultPath }
    func finish(_ clicked: String) {
        if !done {
            done = true
            writeResult(resultPath, clicked)
            NSApp.terminate(nil)
        }
    }
    // 关窗（红钮）视同取消——结果必须落盘，调用方靠结果文件收尾
    func windowWillClose(_ notification: Notification) { finish("cancel") }
}

let args = CommandLine.arguments

if args.contains("--self-test") {
    let tmp = NSTemporaryDirectory() + "alert_panel_selftest.json"
    try? "{\"title\":\"面板自测\",\"message\":\"3 秒后自动关闭——验证编译产物可正常弹窗。\",\"button_cancel\":\"知道了\",\"timeout_secs\":3}".write(toFile: tmp, atomically: true, encoding: .utf8)
    let delegate = runPanel(pendingPath: tmp, resultPath: tmp + ".result")
    print(delegate?.done == true ? "面板已收尾" : "面板未收尾（异常）")
    fflush(stdout)
    exit(0)
}

guard args.count >= 3 else {
    FileHandle.standardError.write("用法：alert_panel <pending_json> <result_json> [--timeout 秒]\n".data(using: .utf8)!)
    exit(1)
}
let pendingPath = args[1]
let resultPath = args[2]

// pending 文件缺失 → 直接写 cancel 结果退出（提醒内容丢了不能挂死调用方轮询）
guard let data = FileManager.default.contents(atPath: pendingPath),
      let pend = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
    writeResult(resultPath, "cancel")
    exit(1)
}
_ = runPanel(pendingPath: pendingPath, resultPath: resultPath,
             overrideTimeout: args.contains("--timeout")
                 ? Int(args[args.firstIndex(of: "--timeout")! + 1]) : nil,
             pending: pend)
exit(0)

// 建面板并进入 App 主循环；返回 delegate 供自测读状态
func runPanel(pendingPath: String, resultPath: String,
              overrideTimeout: Int? = nil, pending: [String: Any]? = nil) -> PanelDelegate? {
    var pend: [String: Any] = pending ?? [:]
    if pending == nil {
        guard let data = FileManager.default.contents(atPath: pendingPath),
              let p = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        pend = p
    }
    let title = pend["title"] as? String ?? "DayTradingAgent"
    let message = (pend["message"] as? String ?? "（无内容）")
        .replacingOccurrences(of: "\\n", with: "\n")
    let okText = pend["button_ok"] as? String ?? "已添加"
    let cancelText = pend["button_cancel"] as? String ?? "取消"
    let single = (cancelText == "none") || (pend["single"] as? Bool == true)
    let timeoutSecs = overrideTimeout ?? (pend["timeout_secs"] as? Int ?? 3600)

    let app = NSApplication.shared
    app.setActivationPolicy(.regular)   // 有 Dock 图标：最小化后点 Dock 唤回

    let style: NSWindow.StyleMask = [.titled, .closable, .miniaturizable]
    let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 360),
                       styleMask: style, backing: .buffered, defer: false)
    win.title = title
    win.center()

    let delegate = PanelDelegate(resultPath: resultPath)
    win.delegate = delegate

    let content = NSView(frame: NSRect(x: 0, y: 0, width: 520, height: 360))

    // 正文：可滚动只读文本（IP 清单可能很长）
    let scroll = NSScrollView(frame: NSRect(x: 16, y: 56, width: 488, height: 288))
    let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 488, height: 288))
    tv.string = message
    tv.isEditable = false
    tv.isSelectable = true
    tv.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
    tv.backgroundColor = NSColor.textBackgroundColor
    tv.autoresizingMask = [.width]
    scroll.documentView = tv
    scroll.hasVerticalScroller = true
    content.addSubview(scroll)

    // 按钮行（右下：取消在左、确认在右；单按钮只留确认）
    let okBtn = NSButton(title: okText, target: nil, action: nil)
    okBtn.bezelStyle = .push
    okBtn.keyEquivalent = "\r"
    okBtn.sizeToFit()
    var x = 504 - okBtn.frame.width
    okBtn.frame.origin = NSPoint(x: x, y: 16)
    content.addSubview(okBtn)

    if !single {
        let cancelBtn = NSButton(title: cancelText, target: nil, action: nil)
        cancelBtn.bezelStyle = .push
        cancelBtn.sizeToFit()
        x -= cancelBtn.frame.width + 12
        cancelBtn.frame.origin = NSPoint(x: x, y: 16)
        content.addSubview(cancelBtn)
        cancelBtn.onAction = { _ in delegate.finish("cancel") }
    }
    okBtn.onAction = { _ in delegate.finish("ok") }

    win.contentView = content
    win.makeKeyAndOrderFront(nil)
    app.activate(ignoringOtherApps: true)

    // 超时自动关：按 timeout 写结果（调用方冷却后重弹，信息不丢）
    DispatchQueue.main.asyncAfter(deadline: .now() + Double(timeoutSecs)) {
        delegate.finish("timeout")
    }

    app.run()
    return delegate
}

// NSButton 闭包扩展（AppKit 无 SwiftUI 的 action 闭包写法）
extension NSButton {
    private struct Assoc { static var key = 0 }
    var onAction: ((NSButton) -> Void)? {
        get { objc_getAssociatedObject(self, &Assoc.key) as? (NSButton) -> Void }
        set {
            objc_setAssociatedObject(self, &Assoc.key, newValue, .OBJC_ASSOCIATION_RETAIN)
            target = newValue == nil ? nil : self
            action = newValue == nil ? nil : #selector(_invoke(_:))
        }
    }
    @objc private func _invoke(_ sender: NSButton) { onAction?(sender) }
}
