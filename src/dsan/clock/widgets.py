"""Small widget fixes shared by the control window and panels."""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import QComboBox, QLabel, QLayout, QSizePolicy, QStyle, QWidget


class Combo(QComboBox):
    """A QComboBox whose drop-down list is always wide enough for its longest item.

    On macOS the list is otherwise only as wide as the closed box, so long names
    ("Confidence monitor", serial adapter names…) are cut off. The width is
    measured each time the list opens, so items added later are covered too.
    """

    def popup_width(self) -> int:
        fm = self.view().fontMetrics()
        text = max((fm.horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0)
        icon = self.iconSize().width() + 6 if any(not self.itemIcon(i).isNull() for i in range(self.count())) else 0
        frame = 2 * self.view().frameWidth()
        scroll = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent) \
            if self.count() > self.maxVisibleItems() else 0
        return text + icon + frame + scroll + 48       # room for the check mark and margins

    def showPopup(self):
        self.view().setMinimumWidth(max(self.width(), self.popup_width()))
        super().showPopup()


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto further rows when there isn't room,
    so a toolbar never forces its window to stay wide."""

    def __init__(self, parent: QWidget = None, h_spacing: int = 6, v_spacing: int = 4):
        super().__init__(parent)
        self._items = []
        self._h, self._v = h_spacing, v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    # QLayout plumbing
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self):
        w = sum(i.sizeHint().width() for i in self._items) + self._h * max(0, len(self._items) - 1)
        h = max((i.sizeHint().height() for i in self._items), default=0)
        m = self.contentsMargins()
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self):
        size = QSize()
        for i in self._items:
            size = size.expandedTo(i.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, apply: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, row_h = r.x(), r.y(), 0
        for item in self._items:
            if item.widget() is not None and not item.widget().isVisibleTo(self.parentWidget()):
                continue
            hint = item.sizeHint()
            if x + hint.width() > r.right() + 1 and row_h > 0:
                x, y, row_h = r.x(), y + row_h + self._v, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
                sep = item.widget().property("flow_sep") if item.widget() is not None else None
                if sep is not None:                       # a divider at the start of a row looks stray
                    sep.setText("" if x == r.x() else "│")
            x += hint.width() + self._h
            row_h = max(row_h, hint.height())
        return y + row_h - rect.y() + m.bottom()


def elide_label(label, text: str) -> None:
    """Show `text` in a label that may be squeezed: it ends in … when narrow, full text in the tooltip."""
    label.setProperty("full_text", text)
    label.setToolTip(text)
    _elide(label)


def _elide(label) -> None:
    text = label.property("full_text") or ""
    label.setText(label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, max(0, label.width())))


class ElidingLabel(QLabel):
    """A QLabel that shortens its text with … instead of forcing its parent wider."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        elide_label(self, text)

    def full_text(self) -> str:
        return self.property("full_text") or ""

    def resizeEvent(self, e):
        super().resizeEvent(e)
        _elide(self)
