import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Panel {
  id: root
  moduleName: "csarmiento.elgato-camerahub"
  ipcTarget: "csarmiento.elgato-camerahub"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property bool openedFromHotkey: false
  readonly property var barIdentity: hostWidget || root

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string icon: hostWidget ? hostWidget.icon : "\uDB80\uDD00"

  readonly property var devices: hostWidget ? hostWidget.devices : []
  readonly property var device: hostWidget ? hostWidget.selectedDevice : null
  readonly property string backend: hostWidget ? hostWidget.backend : "none"
  readonly property bool loaded: hostWidget ? hostWidget.loaded : false
  readonly property bool ok: hostWidget ? hostWidget.ok : true
  readonly property string errorText: hostWidget ? hostWidget.errorText : ""
  readonly property string shownKey: deviceKey(device)

  readonly property var families: [
    "Facecam",
    "Facecam Pro",
    "Facecam 4K",
    "Facecam MK.2",
    "Facecam Neo",
    "Cam Link",
    "Game Capture"
  ]

  readonly property string phase: {
    if (!root.loaded) return "checking"
    if (root.devices.length === 0) return root.ok ? "empty" : "error"
    return "device"
  }

  readonly property bool showingControls: {
    var d = root.device
    if (!d || d.role !== "webcam" || root.backend !== "v4l2-ctl") return false
    if (!/^\/dev\/video[0-9]+$/.test(String(d.video || ""))) return false
    return !!(d.controls && d.controls.length > 0)
  }

  readonly property var controls: (device && device.controls) ? device.controls : []

  readonly property string errorLine: {
    if (!root.loaded || (root.ok && root.errorText === "")) return ""
    if (root.errorText !== "") return root.errorText
    return strings.t("panel.error")
  }

  readonly property string noteLine: (device && device.note) ? device.note : ""

  // Our sentences only when the helper left note empty. A note, when present, is shown as-is.
  readonly property string explanation: {
    var d = root.device
    if (!d || root.noteLine !== "") return ""
    if (d.role === "capture") return strings.t("panel.capture")
    if (root.backend !== "v4l2-ctl" && d.role !== "capture") return strings.t("panel.needV4l")
    if (root.showingControls) return ""
    return strings.t("panel.noControls")
  }

  function deviceKey(d) { return root.hostWidget ? root.hostWidget.deviceKey(d) : "" }

  // Vendor is always 0fd9. The helper sends product only.
  function idLine(d) {
    if (!d) return ""
    var product = String(d.product || "")
    if (product === "") return ""
    return "0fd9:" + product
  }

  function optionLabel(control) {
    if (!control) return ""
    var options = control.options || []
    for (var i = 0; i < options.length; i++) {
      if (String(options[i].value) === String(control.value) && options[i].label) return options[i].label
    }
    if (control.value === null || control.value === undefined) return "—"
    return Commons.I18n.formatNumber(Math.round(Number(control.value)))
  }

  function snap(control, value) {
    var step = control && control.step > 0 ? control.step : 1
    var min = control ? Number(control.min) : 0
    var max = control ? Number(control.max) : 0
    var n = Math.round(Number(value))
    if (isFinite(min) && isFinite(max) && max > min) {
      n = Math.round((n - min) / step) * step + min
      n = Math.max(min, Math.min(max, n))
    }
    return Math.round(n)
  }

  function ranged(control) {
    return !!(control && control.kind === "int" && Number(control.max) > Number(control.min))
  }

  function open() {
    openedFromHotkey = false
    setCenterHoverRevealSuppressed(false)
    root.controller.show()
  }

  function openFromHotkey() {
    openedFromHotkey = true
    root.controller.show()
    Qt.callLater(function() {
      if (root.opened) setCenterHoverRevealSuppressed(true)
    })
  }

  function close() {
    setCenterHoverRevealSuppressed(false)
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.openFromHotkey()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function setCenterHoverRevealSuppressed(value) {
    if (root.bar && typeof root.bar.setCenterHoverRevealSuppressed === "function")
      root.bar.setCenterHoverRevealSuppressed(value)
    else if (root.bar && "centerHoverRevealSuppressed" in root.bar)
      root.bar.centerHoverRevealSuppressed = value
  }

  ShellIpc {
    target: root.ipcTarget

    function open(): void { root.openFromHotkey() }
    function close(): void { root.close() }
    function show(): void { root.openFromHotkey() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(520))

    content: PanelKeyCatcher {
      id: keyCatcher
      implicitHeight: column.implicitHeight
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onShownKeyChanged: scroll.contentY = 0

      property string shownKey: root.shownKey

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height + 1

        Column {
          id: column
          width: scroll.width
          spacing: Style.spacing.panelGap

          Row {
            spacing: Style.space(8)
            PlainText {
              text: root.icon
              font.pixelSize: Style.font.icon
              anchors.verticalCenter: parent.verticalCenter
              opacity: root.phase === "device" ? 1 : 0.6
            }
            PlainText {
              text: strings.t("panel.brand")
              anchors.verticalCenter: parent.verticalCenter
            }
          }

          PlainText {
            visible: root.errorLine !== ""
            width: parent.width
            text: root.errorLine
            color: root.urgent
            font.pixelSize: Style.font.bodySmall
          }

          PlainText {
            visible: root.phase === "checking"
            width: parent.width
            text: strings.t("panel.checking")
            color: root.dim
          }

          Column {
            visible: root.phase === "empty"
            width: parent.width
            spacing: Style.spacing.lg

            PlainText {
              width: parent.width
              text: strings.t("panel.empty")
            }

            PanelSeparator { foreground: root.foreground; strength: 0.08 }

            Repeater {
              model: root.families
              PlainText {
                required property string modelData
                width: column.width
                text: modelData
                color: root.dim
                font.pixelSize: Style.font.caption
              }
            }
          }

          Column {
            visible: root.phase === "device" && root.devices.length > 1
            width: parent.width
            spacing: Style.spacing.sm

            PanelSectionHeader {
              text: strings.t("panel.cameras")
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.devices.length
              Button {
                required property int index
                readonly property var entry: root.devices[index]
                width: column.width
                text: entry ? (entry.name !== "" ? entry.name : root.idLine(entry)) : ""
                selected: !!(entry && root.deviceKey(entry) === root.deviceKey(root.device))
                bordered: true
                leftAlign: true
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.body
                onClicked: if (entry && root.hostWidget) root.hostWidget.pick(root.deviceKey(entry))
              }
            }
          }

          Column {
            visible: root.phase === "device" && root.device
            width: parent.width
            spacing: Style.spacing.lg

            Column {
              width: parent.width
              spacing: Style.spacing.xs

              PlainText {
                width: parent.width
                text: root.device ? root.device.name : ""
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
                wrapMode: Text.NoWrap
              }

              PlainText {
                visible: text !== ""
                text: root.idLine(root.device)
                color: root.dim
                font.pixelSize: Style.font.caption
                font.letterSpacing: 0.6
                LayoutMirroring.enabled: false
              }
            }

            PanelSeparator {
              visible: root.showingControls || root.noteLine !== "" || root.explanation !== ""
              foreground: root.foreground
              strength: 0.1
            }

            PlainText {
              visible: root.noteLine !== ""
              width: parent.width
              text: root.noteLine
              color: root.dim
            }

            PlainText {
              visible: root.explanation !== ""
              width: parent.width
              text: root.explanation
              color: root.dim
            }

            Column {
              visible: root.showingControls
              width: parent.width
              spacing: Style.spacing.xl

              Repeater {
                model: root.controls.length
                ControlRow {
                  required property int index
                  control: root.controls[index]
                }
              }
            }
          }
        }
      }
    }
  }

  component PlainText: Text {
    textFormat: Text.PlainText
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    wrapMode: Text.WordWrap
  }

  component ControlRow: Column {
    id: row
    property var control: null

    readonly property bool writable: !!(control && control.writable === true)
    readonly property string video: root.device ? root.device.video : ""
    readonly property var options: control && control.options ? control.options : []
    readonly property bool showValue: {
      if (!control || control.kind === "bool") return false
      if (control.kind === "menu" && options.length > 0) return false
      return true
    }

    width: parent.width
    spacing: Style.spacing.sm
    enabled: writable
    opacity: writable ? 1 : 0.5

    Item {
      width: parent.width
      implicitHeight: Math.max(nameText.implicitHeight, trailing.implicitHeight)

      PlainText {
        id: nameText
        width: Math.max(0, parent.width - trailing.width - Style.space(8))
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        text: row.control ? row.control.label : ""
        elide: Text.ElideRight
        wrapMode: Text.NoWrap
      }

      Item {
        id: trailing
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        implicitWidth: sw.visible ? sw.implicitWidth : (valueText.visible ? valueText.implicitWidth : 0)
        implicitHeight: sw.visible ? sw.implicitHeight : (valueText.visible ? valueText.implicitHeight : 0)
        width: implicitWidth
        height: implicitHeight

        PlainText {
          id: valueText
          visible: row.showValue
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: row.control ? root.optionLabel(slider.dragging ? ({ value: slider.liveValue, options: row.options }) : row.control) : ""
          color: root.dim
          font.pixelSize: Style.font.caption
          font.bold: true
        }

        ToggleSwitch {
          id: sw
          visible: row.control && row.control.kind === "bool"
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          checked: !!(row.control && row.control.value)
          interactive: row.writable
          foreground: root.foreground
          accent: root.accent
          onToggled: {
            if (!row.control || !root.hostWidget) return
            root.hostWidget.setControl(row.video, row.control.key, row.control.value ? 0 : 1)
          }
        }
      }
    }

    PanelSlider {
      id: slider
      visible: root.ranged(row.control)
      width: parent.width
      bar: root.bar
      minimum: row.control ? row.control.min : 0
      maximum: row.control ? row.control.max : 1
      step: row.control && row.control.step > 0 ? row.control.step : 1
      integer: true
      value: row.control && row.control.value !== null ? row.control.value : 0
      enabled: row.writable
      onMoved: function(v) { if (root.hostWidget) root.hostWidget.hold(800) }
      onReleased: function(v) {
        if (!row.control || !root.hostWidget) return
        root.hostWidget.setControl(row.video, row.control.key, root.snap(row.control, v))
      }
    }

    Flow {
      visible: row.control && row.control.kind === "menu" && row.options.length > 0
      width: parent.width
      spacing: Style.spacing.md

      Repeater {
        model: row.options.length
        Button {
          required property int index
          readonly property var option: row.options[index]
          text: option && option.label !== "" ? option.label : (option ? Commons.I18n.formatNumber(option.value) : "")
          selected: !!(option && row.control && String(option.value) === String(row.control.value))
          bordered: true
          enabled: row.writable
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          horizontalPadding: Style.space(8)
          onClicked: {
            if (!option || !row.control || !root.hostWidget) return
            root.hostWidget.setControl(row.video, row.control.key, option.value)
          }
        }
      }
    }
  }
}
