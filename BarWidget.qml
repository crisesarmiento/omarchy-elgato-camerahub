import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Camera glyph already used for webcam and camera nodes in the audio panel
// (U+F0100). Not an image, and not an Elgato asset.
BarWidget {
  id: root
  moduleName: "csarmiento.elgato-camerahub"

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  readonly property string helperPath: Qt.resolvedUrl("camerahub.py").toString().replace(/^file:\/\//, "")
  readonly property string icon: "\uDB80\uDD00"

  property var hub: ({ ok: true, backend: "none", devices: [] })
  property bool loaded: false
  property bool ok: true
  property string errorText: ""
  property string selectedKey: ""

  property var setQueue: []
  property bool setBusy: false
  property bool setOutDone: false
  property bool setExitDone: false
  property bool setSettled: false
  property string setText: ""
  property int setCode: 0
  property double holdUntil: 0
  property double statusStarted: 0
  property double statusExitMark: 0
  property double setStarted: 0
  property bool statusGotStdout: false

  readonly property var devices: (hub && Array.isArray(hub.devices)) ? hub.devices : []
  readonly property string backend: hub && hub.backend === "v4l2-ctl" ? "v4l2-ctl" : "none"

  readonly property string barLabel: {
    if (root.devices.length === 1) {
      var name = shortBarName(root.devices[0].name)
      if (name !== "") return name
    }
    return strings.t("bar.idle")
  }
  readonly property bool showBarLabel: !root.vertical && root.barLabel !== ""

  readonly property var selectedDevice: {
    var list = root.devices
    if (!list.length) return null
    for (var i = 0; i < list.length; i++) {
      if (deviceKey(list[i]) === root.selectedKey) return list[i]
    }
    return list[0]
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  onOpenedChanged: if (root.opened) root.refresh(true)

  function open() {
    if (panelLoader.item && panelLoader.item.openFromHotkey) panelLoader.item.openFromHotkey()
  }

  function close() {
    if (panelLoader.item && panelLoader.item.close) panelLoader.item.close()
  }

  function togglePanel() {
    if (panelLoader.item && panelLoader.item.toggle) panelLoader.item.toggle()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  function deviceKey(d) {
    if (!d) return ""
    return [d.video || "", d.usb || "", d.product || "", d.name || "", d.role || ""].join("|")
  }

  function pick(key) { root.selectedKey = String(key || "") }

  function hold(ms) {
    var until = Date.now() + (ms || 600)
    if (until > root.holdUntil) root.holdUntil = until
  }

  function clip(value, max) {
    var s = String(value == null ? "" : value).replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim()
    return s.length > max ? s.slice(0, max) : s
  }

  function hostName(value) {
    var s = clip(value, 64)
    if (!/^[0-9.]+$/.test(s)) return ""
    return s
  }

  // sysfs device name, for example "1-2". Never a path, and never shown as one.
  function sysfsName(value) {
    var s = clip(value, 32)
    if (s.indexOf("/") >= 0 || s.indexOf("\\") >= 0 || s.indexOf("..") >= 0) return ""
    return s
  }

  function num(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback
    var n = Number(value)
    return isFinite(n) ? n : fallback
  }

  function shortBarName(name) {
    var s = clip(name, 80)
    var cut = s.replace(/\s+\([^)]*\)\s*$/, "")
    if (cut !== "") s = cut
    if (s.length > 22) s = s.slice(0, 21) + "…"
    return s
  }

  function sanitize(parsed) {
    var raw = Array.isArray(parsed.devices) ? parsed.devices : []
    var devices = []
    var limit = Math.min(raw.length, 8)
    for (var i = 0; i < limit; i++) {
      var d = raw[i]
      if (!d || typeof d !== "object") continue
      var role = String(d.role || "")
      if (role !== "webcam" && role !== "capture" && role !== "wifi") role = ""
      var controls = []
      var rawControls = Array.isArray(d.controls) ? d.controls : []
      var controlLimit = Math.min(rawControls.length, 32)
      for (var j = 0; j < controlLimit; j++) {
        var c = rawControls[j]
        if (!c || typeof c !== "object") continue
        var kind = String(c.kind || "")
        if (kind !== "int" && kind !== "bool" && kind !== "menu") kind = ""
        var options = []
        if (Array.isArray(c.options)) {
          var optionLimit = Math.min(c.options.length, 24)
          for (var k = 0; k < optionLimit; k++) {
            var o = c.options[k]
            if (!o || typeof o !== "object") continue
            options.push({ value: num(o.value, 0), label: clip(o.label, 40) })
          }
        }
        var value = (c.value === null || c.value === undefined || c.value === "") ? null : num(c.value, 0)
        controls.push({
          key: clip(c.key, 64),
          label: clip(c.label, 60) || clip(c.key, 60),
          kind: kind,
          min: num(c.min, 0),
          max: num(c.max, 0),
          step: Math.max(1, Math.round(num(c.step, 1))),
          value: value,
          writable: c.writable === true,
          options: options
        })
      }
      devices.push({
        name: clip(d.name, 80),
        family: clip(d.family, 32),
        role: role,
        product: clip(d.product, 8),
        usb: sysfsName(d.usb),
        video: clip(d.video, 32),
        host: hostName(d.host),
        note: clip(d.note, 160),
        controls: controls
      })
    }
    return {
      ok: true,
      backend: parsed.backend === "v4l2-ctl" ? "v4l2-ctl" : "none",
      devices: devices
    }
  }

  function applyPayload(parsed) {
    root.hub = sanitize(parsed)
    root.ok = true
    root.errorText = ""
    root.loaded = true
  }

  function ingest(raw) {
    var text = String(raw || "").trim()
    if (!text) {
      root.ok = false
      root.errorText = ""
      root.loaded = true
      return
    }
    var parsed = null
    try { parsed = JSON.parse(text) } catch (e) { parsed = null }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || parsed.ok !== true) {
      root.ok = false
      root.errorText = clip(parsed && parsed.error, 180)
      root.loaded = true
      return
    }
    applyPayload(parsed)
  }

  function videoOk(video) { return /^\/dev\/video[0-9]+$/.test(String(video || "")) }
  function keyOk(key) { return /^[A-Za-z0-9_]{1,64}$/.test(String(key || "")) }
  function valueOk(value) { return /^-?[0-9]+$/.test(String(value)) }

  function findDevice(video) {
    for (var i = 0; i < root.devices.length; i++) {
      if (root.devices[i].video === video) return root.devices[i]
    }
    return null
  }

  function findControl(device, key) {
    if (!device || !device.controls) return null
    for (var i = 0; i < device.controls.length; i++) {
      if (device.controls[i].key === key) return device.controls[i]
    }
    return null
  }

  function controlWritable(video, key) {
    if (!videoOk(video) || !keyOk(key)) return false
    var device = findDevice(video)
    if (!device || (device.role !== "webcam" && device.role !== "wifi")) return false
    // Webcam sliders need v4l2-ctl. iPhone picture controls do not.
    if (device.role === "webcam" && root.backend !== "v4l2-ctl") return false
    var control = findControl(device, key)
    return !!(control && control.writable === true)
  }

  function patchLocal(video, key, value) {
    var next = JSON.parse(JSON.stringify(root.hub))
    var list = next.devices || []
    for (var i = 0; i < list.length; i++) {
      if (list[i].video !== video || !list[i].controls) continue
      for (var j = 0; j < list[i].controls.length; j++) {
        if (list[i].controls[j].key === key) list[i].controls[j].value = value
      }
    }
    root.hub = next
  }

  function setControl(video, key, value) {
    var whole = String(Math.round(Number(value)))
    if (!valueOk(whole) || !controlWritable(video, key)) return
    var device = findDevice(video)
    var control = findControl(device, key)
    if (control && control.value !== null && String(Math.round(Number(control.value))) === whole) return
    patchLocal(video, key, Number(whole))
    hold(800)
    var q = root.setQueue.slice()
    var id = String(video) + "\n" + String(key)
    var replaced = false
    for (var i = 0; i < q.length; i++) {
      if (q[i].id === id) {
        q[i] = { id: id, video: String(video), key: String(key), value: whole }
        replaced = true
        break
      }
    }
    if (!replaced) {
      if (q.length >= 8) q.shift()
      q.push({ id: id, video: String(video), key: String(key), value: whole })
    }
    root.setQueue = q
    pumpSet()
  }

  function pumpSet() {
    if (root.setBusy || root.setQueue.length === 0) return
    var q = root.setQueue.slice()
    var job = q.shift()
    root.setQueue = q
    if (!job || !videoOk(job.video) || !keyOk(job.key) || !valueOk(job.value)) {
      pumpSet()
      return
    }
    root.setBusy = true
    root.setOutDone = false
    root.setExitDone = false
    root.setSettled = false
    root.setText = ""
    root.setCode = 0
    root.setStarted = Date.now()
    hold(800)
    setProc.command = ["python3", root.helperPath, "set", job.video, job.key, String(job.value)]
    setProc.running = true
  }

  function preview(video) {
    if (!videoOk(video) || previewProc.running) return
    var device = findDevice(video)
    if (!device || (device.role !== "webcam" && device.role !== "wifi")) return
    previewProc.command = ["python3", root.helperPath, "preview", String(video)]
    previewProc.running = true
  }

  function finishSet() {
    if (!root.setOutDone || !root.setExitDone || root.setSettled) return
    root.setSettled = true
    var raw = String(root.setText || "").trim()
    var parsed = null
    try { parsed = JSON.parse(raw) } catch (e) { parsed = null }
    var succeeded = !!(parsed && parsed.ok === true)
    root.setBusy = false
    if (succeeded) applyPayload(parsed)
    else {
      root.ok = false
      root.errorText = clip(parsed && parsed.error, 180)
      root.loaded = true
    }
    if (root.setQueue.length > 0) {
      pumpSet()
    } else if (!succeeded) {
      root.holdUntil = 0
      root.refresh(true)
    } else {
      root.holdUntil = Date.now() + 400
    }
  }

  // One read at load so the bar can show a connected camera before the
  // panel is opened. The timer below runs only while the panel is open.
  function refresh(force) {
    if (statusProc.running || root.setBusy) return
    if (!force && (root.setQueue.length > 0 || Date.now() < root.holdUntil)) return
    root.statusStarted = Date.now()
    root.statusGotStdout = false
    statusProc.command = ["python3", root.helperPath, "status"]
    statusProc.running = true
  }

  function finishStatus(text) {
    if (root.setStarted > root.statusStarted || Date.now() < root.holdUntil || root.setBusy) return
    ingest(text)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()
  Component.onCompleted: refresh(true)

  FontMetrics {
    id: barMetrics
    font.family: button.fontFamily
    font.pixelSize: button.fontSize
  }

  readonly property int labelSlot: {
    if (!root.showBarLabel) return Style.bar.iconSlot
    var textW = barMetrics.advanceWidth(root.barLabel)
    return Math.min(Style.space(220), Math.ceil(Style.bar.iconCanvas + Style.space(8) + textW + Style.space(12)))
  }

  Process {
    id: statusProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.statusGotStdout = true
        root.finishStatus(text)
      }
    }
    onExited: function(code, status) {
      root.statusExitMark = root.statusStarted
      statusStall.restart()
    }
  }

  // Stdout can land just after exit. This is a process settle, not motion,
  // so it stays a fixed interval when animations are off.
  Timer {
    id: statusStall
    interval: 80
    onTriggered: {
      if (root.statusExitMark !== root.statusStarted || root.statusGotStdout) return
      root.finishStatus("")
    }
  }

  Process {
    id: setProc
    stdout: StdioCollector {
      onStreamFinished: {
        root.setText = text
        root.setOutDone = true
        setStall.stop()
        root.finishSet()
      }
    }
    onExited: function(code, status) {
      root.setCode = code
      root.setExitDone = true
      if (root.setOutDone) root.finishSet()
      else setStall.restart()
    }
  }

  // Toggles the preview window. Output is ignored; it never touches the
  // status or set queues.
  Process { id: previewProc }

  Timer {
    id: setStall
    interval: 80
    onTriggered: {
      if (root.setSettled) return
      if (!root.setOutDone) {
        root.setText = ""
        root.setOutDone = true
      }
      root.finishSet()
    }
  }

  Timer {
    interval: 2000
    repeat: true
    running: root.opened
    onTriggered: root.refresh(false)
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.icon
    labelText: root.showBarLabel ? root.barLabel : ""
    slotSize: root.labelSlot
    opacity: root.loaded && root.devices.length === 0 && !root.opened ? 0.55 : 1
    tooltipText: ""
    onPressed: function(b) {
      if (b === Qt.LeftButton) root.togglePanel()
    }
  }
}
