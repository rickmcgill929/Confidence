"""Small widget fixes shared by the control window and panels."""
from __future__ import annotations

from PyQt6.QtWidgets import QComboBox, QStyle


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
