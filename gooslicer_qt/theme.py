"""Light and dark themes: Fusion with an explicit palette, so the look is the same on every
desktop. "system" follows the desktop's colour-scheme preference (GNOME/KDE, via Qt's style
hints, which read the XDG settings portal) and switches live when it changes."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

THEMES = (("system", "&System"), ("light", "&Light"), ("dark", "&Dark"))
DEFAULT = "system"
WARNING = {"light": "#c0392b", "dark": "#ff7b72"}     # warning text, readable on each background

# Matches the 3D viewport's background (0.16, 0.17, 0.19) so the window and view sit together.
_DARK = {
    QPalette.Window: "#2d3036",
    QPalette.WindowText: "#e3e5e8",
    QPalette.Base: "#202226",
    QPalette.AlternateBase: "#292c31",
    QPalette.ToolTipBase: "#2d3036",
    QPalette.ToolTipText: "#e3e5e8",
    QPalette.PlaceholderText: "#8b9099",
    QPalette.Text: "#e3e5e8",
    QPalette.Button: "#363a41",
    QPalette.ButtonText: "#e3e5e8",
    QPalette.BrightText: "#ff6b6b",
    QPalette.Light: "#4a4f58",
    QPalette.Midlight: "#3e424a",
    QPalette.Mid: "#26292d",
    QPalette.Dark: "#1d1f22",
    QPalette.Shadow: "#121314",
    QPalette.Highlight: "#3d7ab8",
    QPalette.HighlightedText: "#ffffff",
    QPalette.Link: "#6cb4ee",
    QPalette.LinkVisited: "#b48ee8",
}
_DARK_DISABLED = {
    QPalette.WindowText: "#7c818a",
    QPalette.Text: "#7c818a",
    QPalette.ButtonText: "#7c818a",
    QPalette.Base: "#26292d",
    QPalette.Button: "#30333a",
    QPalette.Highlight: "#4a4f58",
    QPalette.HighlightedText: "#a0a4ab",
}


def dark_palette() -> QPalette:
    p = QPalette()
    for role, c in _DARK.items():
        p.setColor(role, QColor(c))
    if hasattr(QPalette, "Accent"):
        p.setColor(QPalette.Accent, QColor(_DARK[QPalette.Highlight]))
    for role, c in _DARK_DISABLED.items():
        p.setColor(QPalette.Disabled, role, QColor(c))
    return p


def light_palette() -> QPalette:
    return QStyleFactory.create("Fusion").standardPalette()


def system_is_dark(app: QApplication) -> bool:
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


def resolve(mode: str, app: QApplication) -> str:
    """'light' or 'dark' for a theme setting."""
    if mode == "system":
        return "dark" if system_is_dark(app) else "light"
    return mode if mode in ("light", "dark") else resolve(DEFAULT, app)


class ThemeManager(QObject):
    """Applies a theme to the application and keeps 'system' in step with the desktop."""
    changed = Signal(str)       # "light" or "dark", after the palette is applied

    def __init__(self, app: QApplication, mode: str = DEFAULT):
        super().__init__(app)
        self.app = app
        self.mode = mode if mode in dict(THEMES) else DEFAULT
        self.current = ""
        app.styleHints().colorSchemeChanged.connect(self._system_changed)
        self.apply(self.mode)

    def apply(self, mode: str) -> str:
        self.mode = mode if mode in dict(THEMES) else DEFAULT
        if self.app.style().name().lower() != "fusion":
            self.app.setStyle("Fusion")
        self.current = resolve(self.mode, self.app)
        self.app.setPalette(dark_palette() if self.current == "dark" else light_palette())
        self.changed.emit(self.current)
        return self.current

    def warning_color(self) -> str:
        return WARNING[self.current or "light"]

    @classmethod
    def for_app(cls, app: QApplication, mode: str = DEFAULT) -> "ThemeManager":
        """The application's one manager, created on first use, with `mode` applied."""
        tm = app.property("gooslicerTheme")
        if not isinstance(tm, cls):
            tm = cls(app, mode)
            app.setProperty("gooslicerTheme", tm)
        else:
            tm.apply(mode)
        return tm

    @property
    def is_dark(self) -> bool:
        return self.current == "dark"

    def _system_changed(self, _scheme) -> None:
        if self.mode == "system":
            self.apply("system")
