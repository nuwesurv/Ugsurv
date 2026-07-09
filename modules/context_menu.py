from qgis.PyQt.QtWidgets import QMenu, QAction
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject, QgsFeature


_STYLE = """
QMenu {
    background-color: #252526;
    color: #d4d4d4;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    padding: 3px 0px;
    font-size: 10pt;
}
QMenu::item {
    padding: 5px 28px 5px 18px;
}
QMenu::item:selected {
    background-color: #0078d4;
    color: #ffffff;
}
QMenu::item:disabled {
    color: #555;
}
QMenu::separator {
    height: 1px;
    background: #3c3c3c;
    margin: 3px 10px;
}
QMenu::right-arrow {
    width: 6px;
    height: 6px;
}
"""


class RightClickMenu:
    """Right-click context menu shown on the canvas when in idle/feature/gripped state."""

    def __init__(self, canvas, terminal_dock):
        self.canvas = canvas
        self.terminal_dock = terminal_dock
        self._isolated_layers = {}   # {layer_id: was_visible}
        self._is_isolated = False
        self._clipboard_layer = None
        self._clipboard_features = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def show(self, screen_pos, sel_layer=None, sel_fid=None,
             on_cancel=None, on_select_similar=None):
        """
        Show the context menu.

        screen_pos         – QPoint in canvas (widget) coordinates
        sel_layer          – active QgsVectorLayer or None
        sel_fid            – selected feature id (int) or None
        on_cancel          – callable; triggered by "Clear Selection" when shown
        on_select_similar  – callable; called when "Select Similar" is triggered
        """
        menu = QMenu(self.canvas)
        menu.setStyleSheet(_STYLE)

        # 1. Recent Input
        self._add_recent_input(menu)

        menu.addSeparator()

        # 2. Isolate Objects
        self._add_isolate(menu, sel_layer)

        menu.addSeparator()

        # 3. Clipboard
        self._add_clipboard(menu, sel_layer, sel_fid)

        menu.addSeparator()

        # 4. Display Order
        self._add_display_order(menu, sel_layer)

        # 5. Select Similar — only when a feature is active
        if sel_layer is not None and sel_fid is not None and on_select_similar is not None:
            menu.addSeparator()
            act = QAction("Select Similar", menu)
            act.triggered.connect(lambda _: on_select_similar())
            menu.addAction(act)

        # Show at canvas-global position
        menu.exec_(self.canvas.mapToGlobal(screen_pos))

    # ------------------------------------------------------------------
    # 1. Recent Input
    # ------------------------------------------------------------------

    def _add_recent_input(self, menu):
        sub = menu.addMenu("Recent Input")
        history = [
            h for h in reversed(self.terminal_dock.commandHistory)
            if h.strip()
        ][:8]
        if not history:
            none_act = QAction("(no history)", sub)
            none_act.setEnabled(False)
            sub.addAction(none_act)
        else:
            for cmd in history:
                act = QAction(cmd.upper(), sub)
                act.triggered.connect(lambda checked=False, c=cmd: self._run_command(c))
                sub.addAction(act)

    def _run_command(self, cmd):
        self.terminal_dock.command.setText(cmd)
        self.terminal_dock.command.returnPressed.emit()

    # ------------------------------------------------------------------
    # 2. Isolate Objects
    # ------------------------------------------------------------------

    def _add_isolate(self, menu, sel_layer):
        if self._is_isolated:
            act = QAction("End Isolation", menu)
            act.triggered.connect(self._end_isolation)
        else:
            act = QAction("Isolate Layer", menu)
            if sel_layer is not None:
                act.triggered.connect(lambda: self._isolate_layer(sel_layer))
            else:
                act.setEnabled(False)
        menu.addAction(act)

    def _isolate_layer(self, sel_layer):
        root = QgsProject.instance().layerTreeRoot()
        self._isolated_layers = {}
        target_id = sel_layer.id()
        for node in root.findLayers():
            lid = node.layerId()
            self._isolated_layers[lid] = node.isVisible()
            node.setItemVisibilityChecked(lid == target_id)
        self._is_isolated = True

    def _end_isolation(self):
        root = QgsProject.instance().layerTreeRoot()
        for node in root.findLayers():
            lid = node.layerId()
            if lid in self._isolated_layers:
                node.setItemVisibilityChecked(self._isolated_layers[lid])
        self._isolated_layers = {}
        self._is_isolated = False

    # ------------------------------------------------------------------
    # 3. Clipboard
    # ------------------------------------------------------------------

    def _add_clipboard(self, menu, sel_layer, sel_fid):
        sub = menu.addMenu("Clipboard")
        has_sel = sel_layer is not None and sel_fid is not None
        has_clip = bool(self._clipboard_features) and self._clipboard_layer is not None

        copy_act = QAction("Copy", sub)
        copy_act.setEnabled(has_sel)
        if has_sel:
            copy_act.triggered.connect(lambda: self._copy(sel_layer, sel_fid))
        sub.addAction(copy_act)

        cut_act = QAction("Cut", sub)
        cut_act.setEnabled(has_sel)
        if has_sel:
            cut_act.triggered.connect(lambda: self._cut(sel_layer, sel_fid))
        sub.addAction(cut_act)

        paste_act = QAction("Paste", sub)
        paste_act.setEnabled(has_clip)
        if has_clip:
            paste_act.triggered.connect(self._paste)
        sub.addAction(paste_act)

    def _copy(self, layer, fid):
        feat = layer.getFeature(fid)
        if feat.isValid():
            self._clipboard_layer = layer
            self._clipboard_features = [QgsFeature(feat)]

    def _cut(self, layer, fid):
        self._copy(layer, fid)
        was_editing = layer.isEditable()
        if not was_editing:
            layer.startEditing()
        layer.deleteFeature(fid)
        if not was_editing:
            layer.commitChanges()
        self.canvas.refresh()

    def _paste(self):
        if not self._clipboard_features or self._clipboard_layer is None:
            return
        target = self._clipboard_layer
        was_editing = target.isEditable()
        if not was_editing:
            target.startEditing()
        for feat in self._clipboard_features:
            target.addFeature(QgsFeature(feat))
        if not was_editing:
            target.commitChanges()
        self.canvas.refresh()

    # ------------------------------------------------------------------
    # 4. Display Order
    # ------------------------------------------------------------------

    def _add_display_order(self, menu, sel_layer):
        sub = menu.addMenu("Display Order")
        has_layer = sel_layer is not None

        for label, direction in [
            ("Bring to Front",  'front'),
            ("Bring Forward",   'forward'),
            ("Send Backward",   'backward'),
            ("Send to Back",    'back'),
        ]:
            act = QAction(label, sub)
            act.setEnabled(has_layer)
            if has_layer:
                act.triggered.connect(
                    lambda checked=False, d=direction: self._layer_order(sel_layer, d)
                )
            sub.addAction(act)

    def _layer_order(self, layer, direction):
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer.id())
        if node is None:
            return
        parent = node.parent()
        children = parent.children()
        idx = next((i for i, c in enumerate(children) if c == node), None)
        if idx is None:
            return
        n = len(children)

        if direction == 'front':
            new_idx = 0
        elif direction == 'back':
            new_idx = n - 1
        elif direction == 'forward':
            new_idx = max(0, idx - 1)
        elif direction == 'backward':
            new_idx = min(n - 1, idx + 1)
        else:
            return

        if new_idx == idx:
            return

        clone = node.clone()
        parent.insertChildNode(new_idx, clone)
        parent.removeChildNode(node)

