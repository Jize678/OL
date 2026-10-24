"""深色主题。参考专业修图软件的配色：低饱和背景 + 高对比画布。"""

from __future__ import annotations

COLORS = {
    "bg": "#1b1d21",
    "panel": "#232630",
    "panel_alt": "#282c37",
    "border": "#333844",
    "canvas": "#141518",
    "text": "#e6e8ec",
    "text_dim": "#9aa1ad",
    "accent": "#4c8dff",
    "accent_dim": "#2f5fbf",
    "agent": "#43c59e",
    "human": "#c0a36e",
    "danger": "#e06c75",
    "warning": "#e0b34e",
}


def stylesheet() -> str:
    c = COLORS
    return f"""
    QWidget {{
        background: {c['bg']};
        color: {c['text']};
        font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
        font-size: 12px;
    }}
    QMainWindow::separator {{ background: {c['border']}; width: 1px; height: 1px; }}

    QMenuBar {{ background: {c['panel']}; border-bottom: 1px solid {c['border']}; }}
    QMenuBar::item {{ padding: 6px 12px; background: transparent; }}
    QMenuBar::item:selected {{ background: {c['panel_alt']}; }}
    QMenu {{ background: {c['panel']}; border: 1px solid {c['border']}; padding: 4px; }}
    QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {c['accent_dim']}; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}

    QToolBar {{ background: {c['panel']}; border-bottom: 1px solid {c['border']};
                spacing: 4px; padding: 4px 6px; }}
    QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 5px;
                   padding: 5px 10px; }}
    QToolButton:hover {{ background: {c['panel_alt']}; border-color: {c['border']}; }}
    QToolButton:checked {{ background: {c['accent_dim']}; border-color: {c['accent']}; }}
    QToolButton:disabled {{ color: {c['text_dim']}; }}

    QStatusBar {{ background: {c['panel']}; border-top: 1px solid {c['border']}; }}
    QStatusBar::item {{ border: none; }}

    QDockWidget {{ titlebar-close-icon: none; font-weight: 600; }}
    QDockWidget::title {{ background: {c['panel_alt']}; padding: 6px 10px;
                          border-bottom: 1px solid {c['border']}; }}

    QGroupBox {{ border: 1px solid {c['border']}; border-radius: 6px; margin-top: 16px;
                 padding: 12px 10px 10px 10px; background: {c['panel']}; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                        color: {c['text_dim']}; font-weight: 600; }}

    QSlider::groove:horizontal {{ height: 3px; background: {c['border']}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {c['accent_dim']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {c['text']}; width: 12px; height: 12px;
                                  margin: -5px 0; border-radius: 6px; }}
    QSlider::handle:horizontal:hover {{ background: {c['accent']}; }}
    QSlider::handle:horizontal:pressed {{ background: {c['accent']}; }}

    QDoubleSpinBox, QSpinBox, QLineEdit {{
        background: {c['panel_alt']}; border: 1px solid {c['border']};
        border-radius: 4px; padding: 3px 6px; selection-background-color: {c['accent']};
    }}
    QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {c['accent']}; }}

    QPushButton {{ background: {c['panel_alt']}; border: 1px solid {c['border']};
                   border-radius: 5px; padding: 5px 12px; }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:pressed {{ background: {c['accent_dim']}; }}
    QPushButton:disabled {{ color: {c['text_dim']}; }}
    QPushButton#primary {{ background: {c['accent_dim']}; border-color: {c['accent']}; }}

    QPlainTextEdit, QTextEdit {{ background: {c['canvas']}; border: 1px solid {c['border']};
                                 border-radius: 4px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['accent_dim']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 5px; min-width: 30px; }}

    QSplitter::handle {{ background: {c['border']}; }}
    QToolTip {{ background: {c['panel_alt']}; color: {c['text']};
                border: 1px solid {c['border']}; padding: 4px; }}
    """
