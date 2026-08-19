pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import PflmPrep 1.0

ApplicationWindow {
    id: root
    width: 1360
    height: 920
    minimumWidth: 1160
    minimumHeight: 780
    visible: true
    title: "Exposure prep  ·  UV Laser PFLM"
    color: theme.appBg

    property string initialFile: ""
    onInitialFileChanged: if (initialFile !== "") root.applyLoad(bridge.loadPath(initialFile))

    QtObject {
        id: theme
        readonly property color appBg:        "#202020"
        readonly property color cardBg:       "#2b2b2b"
        readonly property color cardStroke:   "#363636"
        readonly property color surfaceBg:    "#191919"
        readonly property color textPrimary:  "#ffffff"
        readonly property color textSecond:   "#c5c5c5"
        readonly property color textTertiary: "#8a8a8a"
        readonly property color accent:       "#4cc2ff"
        readonly property color ok:           "#6ccb5f"
        readonly property color danger:       "#ff99a4"
        readonly property int   radius:       8
        readonly property int   gap:          12
        readonly property int   pad:          12
        readonly property string face:        "Segoe UI Variable Text"
        readonly property string mono:        "Cascadia Mono"
    }

    property string sourcePath: ""
    property string outputPath: ""
    property string pinfinSelector: ""
    property string bboxSelector: ""
    property string alignSelector: ""

    function params() {
        return {
            "input": root.sourcePath,
            "output": root.outputPath,
            "pinfin": root.pinfinSelector,
            "bbox": root.bboxSelector,
            "align": root.alignSelector,
            "rotation": rotationCombo.currentText,
            "withinRowStride": parseInt(strideField.text) || 2,
            "backside": backsideBox.checked,
            "globalX": parseFloat(offsetX.text) || 0.0,
            "globalY": parseFloat(offsetY.text) || 0.0
        }
    }

    function refresh() {
        if (root.sourcePath !== "")
            bridge.refreshPreview(root.params())
    }

    function applyLoad(info) {
        if (!info || !info.ok)
            return
        if (info.path !== undefined)
            root.sourcePath = info.path
        if (info.suggestedOutput !== undefined)
            root.outputPath = info.suggestedOutput
        if (info.pinfinRow !== undefined && info.pinfinRow >= 0) {
            pinfinCombo.currentIndex = info.pinfinRow
            root.pinfinSelector = bridge.selectorAt(info.pinfinRow)
        }
        if (info.bboxRow !== undefined && info.bboxRow >= 0) {
            bboxCombo.currentIndex = info.bboxRow
            root.bboxSelector = bridge.selectorAt(info.bboxRow)
        }
        if (info.alignRow !== undefined && info.alignRow >= 0) {
            alignCombo.currentIndex = info.alignRow
            root.alignSelector = bridge.selectorAt(info.alignRow)
        }
        root.refresh()
    }

    Component.onCompleted: bridge.attachPreview(preview)

    FileDialog {
        id: openDialog
        title: "Select the PFLM design GDS"
        nameFilters: ["Layout files (*.gds *.oas *.dxf)", "All files (*)"]
        onAccepted: root.applyLoad(bridge.loadFile(selectedFile))
    }

    Dialog {
        id: saveDialog
        title: "Save dataset"
        anchors.centerIn: parent
        modal: true
        standardButtons: Dialog.Save | Dialog.Cancel
        onAccepted: {
            bridge.saveDataset(nameField.text, root.params())
            datasetCombo.currentIndex = bridge.datasetNames.indexOf(nameField.text)
        }
        ColumnLayout {
            spacing: 8
            Label { text: "Name this dataset"; color: theme.textSecond; font.family: theme.face }
            TextField {
                id: nameField
                implicitWidth: 320
                placeholderText: "e.g. 081026 PFLM heaters"
                font.family: theme.face
            }
        }
    }

    // ------------------------------------------------------------- header

    header: Rectangle {
        height: 62
        color: theme.appBg
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1
                    color: theme.cardStroke }
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: theme.pad
            anchors.rightMargin: theme.pad
            spacing: theme.gap

            ColumnLayout {
                spacing: 0
                Label {
                    text: "PFLM exposure prep"
                    color: theme.textPrimary
                    font.family: theme.face
                    font.pixelSize: 17
                    font.weight: Font.DemiBold
                }
                Label {
                    text: bridge.geometrySummary
                    color: theme.textTertiary
                    font.family: theme.face
                    font.pixelSize: 12
                }
            }

            Item { Layout.fillWidth: true }

            Label {
                text: "Dataset"; color: theme.textTertiary
                font.family: theme.face; font.pixelSize: 12
            }
            ComboBox {
                id: datasetCombo
                implicitWidth: 220
                model: bridge.datasetNames
                displayText: currentIndex < 0 ? "none" : currentText
                onActivated: root.applyLoad(bridge.loadDataset(currentText))
            }
            Button {
                text: "Save"
                onClicked: {
                    nameField.text = datasetCombo.currentIndex >= 0 ? datasetCombo.currentText : ""
                    saveDialog.open()
                }
            }
            Button {
                text: "Delete"
                enabled: datasetCombo.currentIndex >= 0
                onClicked: {
                    bridge.deleteDataset(datasetCombo.currentText)
                    datasetCombo.currentIndex = -1
                }
            }

            Rectangle { width: 1; height: 28; color: theme.cardStroke }

            BusyIndicator {
                running: bridge.busy
                visible: bridge.busy
                implicitWidth: 22
                implicitHeight: 22
            }
            Button {
                text: "Build set"
                highlighted: true
                enabled: root.sourcePath !== "" && !bridge.busy
                onClicked: bridge.runBuild(root.params())
            }
        }
    }

    // --------------------------------------------------------------- body

    RowLayout {
        anchors.fill: parent
        anchors.margins: theme.pad
        spacing: theme.gap

        // ================= left column =================
        ColumnLayout {
            Layout.preferredWidth: 372
            Layout.minimumWidth: 352
            Layout.maximumWidth: 372
            Layout.fillHeight: true
            spacing: theme.gap

            ScrollView {
                id: leftPane
                Layout.fillWidth: true
                Layout.fillHeight: true
                contentWidth: availableWidth
                clip: true

                ColumnLayout {
                    width: leftPane.availableWidth
                    spacing: theme.gap

                    // ---- source
                    Rectangle {
                        Layout.fillWidth: true
                        color: theme.cardBg
                        radius: theme.radius
                        border.color: theme.cardStroke
                        border.width: 1
                        implicitHeight: sourceCol.implicitHeight + theme.pad * 2
                        ColumnLayout {
                            id: sourceCol
                            anchors.fill: parent
                            anchors.margins: theme.pad
                            spacing: 8
                            Label {
                                text: "SOURCE"; color: theme.textTertiary
                                font.family: theme.face; font.pixelSize: 11
                                font.weight: Font.DemiBold; font.letterSpacing: 0.6
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                TextField {
                                    Layout.fillWidth: true
                                    text: root.sourcePath
                                    placeholderText: "No file selected"
                                    readOnly: true
                                    font.family: theme.face
                                    font.pixelSize: 12
                                }
                                Button { text: "Browse"; onClicked: openDialog.open() }
                            }
                        }
                    }

                    // ---- layers
                    Rectangle {
                        Layout.fillWidth: true
                        color: theme.cardBg
                        radius: theme.radius
                        border.color: theme.cardStroke
                        border.width: 1
                        implicitHeight: layerCol.implicitHeight + theme.pad * 2
                        ColumnLayout {
                            id: layerCol
                            anchors.fill: parent
                            anchors.margins: theme.pad
                            spacing: 8

                            Label {
                                text: "LAYERS"; color: theme.textTertiary
                                font.family: theme.face; font.pixelSize: 11
                                font.weight: Font.DemiBold; font.letterSpacing: 0.6
                            }

                            Label {
                                text: "Pinfin layer (geometry to expose)"; color: theme.textSecond
                                font.family: theme.face; font.pixelSize: 13
                            }
                            ComboBox {
                                id: pinfinCombo
                                Layout.fillWidth: true
                                model: bridge.layerModel
                                textRole: "label"
                                enabled: count > 0
                                onActivated: {
                                    root.pinfinSelector = bridge.selectorAt(currentIndex)
                                    root.refresh()
                                }
                            }

                            Label {
                                text: "Bbox layer (one box = one array)"; color: theme.textSecond
                                font.family: theme.face; font.pixelSize: 13
                                Layout.topMargin: 4
                            }
                            ComboBox {
                                id: bboxCombo
                                Layout.fillWidth: true
                                model: bridge.layerModel
                                textRole: "label"
                                enabled: count > 0
                                onActivated: {
                                    root.bboxSelector = bridge.selectorAt(currentIndex)
                                    root.refresh()
                                }
                            }

                            Label {
                                text: "Align layer (reference marks)"; color: theme.textSecond
                                font.family: theme.face; font.pixelSize: 13
                                Layout.topMargin: 4
                            }
                            ComboBox {
                                id: alignCombo
                                Layout.fillWidth: true
                                model: bridge.layerModel
                                textRole: "label"
                                enabled: count > 0
                                onActivated: {
                                    root.alignSelector = bridge.selectorAt(currentIndex)
                                    root.refresh()
                                }
                            }
                        }
                    }

                    // ---- rotation & masking
                    Rectangle {
                        Layout.fillWidth: true
                        color: theme.cardBg
                        radius: theme.radius
                        border.color: theme.cardStroke
                        border.width: 1
                        implicitHeight: rotCol.implicitHeight + theme.pad * 2
                        ColumnLayout {
                            id: rotCol
                            anchors.fill: parent
                            anchors.margins: theme.pad
                            spacing: 8

                            Label {
                                text: "ROTATION & MASKING"; color: theme.textTertiary
                                font.family: theme.face; font.pixelSize: 11
                                font.weight: Font.DemiBold; font.letterSpacing: 0.6
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                Label {
                                    text: "Design rotation"; color: theme.textSecond
                                    font.family: theme.face; font.pixelSize: 13
                                }
                                Item { Layout.fillWidth: true }
                                ComboBox {
                                    id: rotationCombo
                                    model: ["auto", "0", "90", "180", "270"]
                                    Layout.preferredWidth: 120
                                    onActivated: root.refresh()
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                text: "Rotate the design (not the wafer) so each row's long "
                                      + "sweep rides stage-X and rows advance along stage-Y. "
                                      + "Auto keeps every stage-Y target under the +6.95 mm ceiling."
                                color: theme.textTertiary
                                font.family: theme.face; font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                Layout.topMargin: 4
                                Label {
                                    text: "Within-row stride"; color: theme.textSecond
                                    font.family: theme.face; font.pixelSize: 13
                                }
                                Item { Layout.fillWidth: true }
                                TextField {
                                    id: strideField
                                    text: "2"
                                    Layout.preferredWidth: 64
                                    validator: IntValidator { bottom: 1 }
                                    font.family: theme.face
                                    font.pixelSize: 12
                                    onEditingFinished: root.refresh()
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                text: "2 = expose every other array (checkerboard), mask, "
                                      + "then the skipped ones. 1 = whole row, one mask."
                                color: theme.textTertiary
                                font.family: theme.face; font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }

                            // live stage-feasibility readout
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.topMargin: 4
                                radius: 6
                                color: theme.surfaceBg
                                border.width: 1
                                border.color: bridge.feasible ? theme.cardStroke : theme.danger
                                implicitHeight: feasCol.implicitHeight + 16
                                ColumnLayout {
                                    id: feasCol
                                    anchors.fill: parent
                                    anchors.margins: 8
                                    spacing: 3
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Rectangle {
                                            width: 8; height: 8; radius: 4
                                            color: bridge.feasible ? theme.ok : theme.danger
                                        }
                                        Label {
                                            text: bridge.feasible ? "STAGE FEASIBLE"
                                                                  : "STAGE INFEASIBLE"
                                            color: bridge.feasible ? theme.ok : theme.danger
                                            font.family: theme.face; font.pixelSize: 12
                                            font.weight: Font.DemiBold
                                        }
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: preview.caption !== "" ? preview.caption
                                              : "Load a source and pick layers to check reachability."
                                        color: theme.textTertiary
                                        font.family: theme.face; font.pixelSize: 11
                                        wrapMode: Text.WordWrap
                                    }
                                }
                            }
                        }
                    }

                    // ---- output & options
                    Rectangle {
                        Layout.fillWidth: true
                        color: theme.cardBg
                        radius: theme.radius
                        border.color: theme.cardStroke
                        border.width: 1
                        implicitHeight: outCol.implicitHeight + theme.pad * 2
                        ColumnLayout {
                            id: outCol
                            anchors.fill: parent
                            anchors.margins: theme.pad
                            spacing: 8
                            Label {
                                text: "OUTPUT"; color: theme.textTertiary
                                font.family: theme.face; font.pixelSize: 11
                                font.weight: Font.DemiBold; font.letterSpacing: 0.6
                            }
                            TextField {
                                Layout.fillWidth: true
                                text: root.outputPath
                                placeholderText: "Set name (folder under output/sets/)"
                                font.family: theme.face
                                font.pixelSize: 12
                                onEditingFinished: root.outputPath = text
                            }
                            Label {
                                Layout.fillWidth: true
                                text: "Writes output/sets/<name>/ with plan.json, jobs/*.dxf, "
                                      + "manifest.csv and prep_log.txt (overwritten in place)."
                                color: theme.textTertiary
                                font.family: theme.face; font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }

                            CheckBox {
                                id: backsideBox
                                text: "Backside exposure (flip)"
                                checked: true
                                Layout.topMargin: 4
                            }

                            Label {
                                text: "Extra global offset, all jobs (µm)"; color: theme.textSecond
                                font.family: theme.face; font.pixelSize: 13
                                Layout.topMargin: 4
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 6
                                Label {
                                    text: "X"; color: theme.textTertiary
                                    font.family: theme.face; font.pixelSize: 12
                                }
                                TextField {
                                    id: offsetX
                                    text: "-3447"
                                    Layout.preferredWidth: 74
                                    validator: DoubleValidator { decimals: 3 }
                                    font.family: theme.face
                                    onEditingFinished: root.refresh()
                                }
                                Label {
                                    text: "Y"; color: theme.textTertiary
                                    font.family: theme.face; font.pixelSize: 12
                                }
                                TextField {
                                    id: offsetY
                                    text: "460"
                                    Layout.preferredWidth: 74
                                    validator: DoubleValidator { decimals: 3 }
                                    font.family: theme.face
                                    onEditingFinished: root.refresh()
                                }
                                Item { Layout.fillWidth: true }
                            }
                            Label {
                                Layout.fillWidth: true
                                text: "Treat all calibration numbers as unverified starting "
                                      + "points — confirm on the rig."
                                color: theme.textTertiary
                                font.family: theme.face; font.pixelSize: 11
                                wrapMode: Text.WordWrap
                            }
                        }
                    }
                }
            }
        }

        // ================= centre =================
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: theme.gap

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: theme.cardBg
                radius: theme.radius
                border.color: theme.cardStroke
                border.width: 1

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: theme.pad
                    spacing: 10

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Label {
                            text: "PREVIEW"; color: theme.textTertiary
                            font.family: theme.face; font.pixelSize: 11
                            font.weight: Font.DemiBold; font.letterSpacing: 0.6
                        }
                        Item { Layout.fillWidth: true }
                        CheckBox {
                            text: "Wafer guide"
                            checked: preview.waferGuide
                            visible: preview.mode === "wafer"
                            onToggled: preview.waferGuide = checked
                        }
                        Button {
                            text: "Wafer"
                            checkable: true
                            checked: preview.mode === "wafer"
                            highlighted: preview.mode === "wafer"
                            onClicked: preview.mode = "wafer"
                        }
                        Button {
                            text: "Field (selected)"
                            checkable: true
                            checked: preview.mode === "field"
                            highlighted: preview.mode === "field"
                            onClicked: preview.mode = "field"
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: 6
                        color: theme.surfaceBg
                        border.color: theme.cardStroke
                        border.width: 1
                        clip: true
                        PreviewItem {
                            id: preview
                            anchors.fill: parent
                            anchors.margins: 1
                        }
                    }

                    // ---- schedule step walker
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Button {
                            text: "‹ Prev"
                            enabled: preview.stepCount > 0 && preview.step > 0
                            onClicked: preview.step = preview.step - 1
                        }
                        Button {
                            text: "Next ›"
                            enabled: preview.stepCount > 0 && preview.step < preview.stepCount - 1
                            onClicked: preview.step = preview.step + 1
                        }
                        Slider {
                            id: stepSlider
                            Layout.fillWidth: true
                            from: 0
                            to: Math.max(preview.stepCount - 1, 0)
                            stepSize: 1
                            enabled: preview.stepCount > 0
                            value: preview.step
                            onMoved: preview.step = Math.round(value)
                        }
                        Label {
                            text: preview.stepCount > 0
                                  ? ("step " + (preview.step + 1) + " / " + preview.stepCount)
                                  : "no schedule"
                            color: theme.textTertiary
                            font.family: theme.mono; font.pixelSize: 12
                        }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: preview.stepLabel
                        color: preview.stepLabel.indexOf("MASK") === 0 ? theme.accent
                               : (preview.stepLabel.indexOf("EMPTY") >= 0
                                  || preview.stepLabel.indexOf("OUT OF FIELD") >= 0)
                                 ? theme.danger : theme.textSecond
                        font.family: theme.face
                        font.pixelSize: 12
                        elide: Text.ElideRight
                    }

                    Repeater {
                        model: bridge.notes
                        delegate: RowLayout {
                            id: noteRow
                            required property string modelData
                            Layout.fillWidth: true
                            spacing: 6
                            Rectangle {
                                width: 3; height: 14; radius: 1.5
                                color: theme.danger
                                Layout.alignment: Qt.AlignVCenter
                            }
                            Label {
                                text: noteRow.modelData
                                color: theme.danger
                                font.family: theme.face
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 156
                color: theme.cardBg
                radius: theme.radius
                border.color: theme.cardStroke
                border.width: 1
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: theme.pad
                    spacing: 8
                    RowLayout {
                        Layout.fillWidth: true
                        Label {
                            text: "LOG"; color: theme.textTertiary
                            font.family: theme.face; font.pixelSize: 11
                            font.weight: Font.DemiBold; font.letterSpacing: 0.6
                        }
                        Item { Layout.fillWidth: true }
                        Label {
                            text: bridge.status
                            color: theme.textSecond
                            font.family: theme.face
                            font.pixelSize: 12
                            elide: Text.ElideRight
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: 6
                        color: theme.surfaceBg
                        border.color: theme.cardStroke
                        border.width: 1
                        clip: true
                        ScrollView {
                            anchors.fill: parent
                            anchors.margins: 8
                            TextArea {
                                id: logArea
                                readOnly: true
                                wrapMode: TextArea.NoWrap
                                color: "#a8d8a8"
                                background: null
                                font.family: theme.mono
                                font.pixelSize: 12
                            }
                        }
                    }
                }
            }
        }
    }

    Connections {
        target: bridge
        function onLogAppended(chunk) {
            logArea.text += chunk
            logArea.cursorPosition = logArea.length
        }
        function onLogCleared() { logArea.text = "" }
    }
}
