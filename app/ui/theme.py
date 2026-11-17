from __future__ import annotations

import re

from PySide6.QtGui import QColor

from app.core.config import config


_RGBA_RE = re.compile(
    r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)",
    re.IGNORECASE,
)


def qcolor(value: str, default_alpha: int = 255) -> QColor:
    if not value:
        return QColor(255, 255, 255, default_alpha)

    m = _RGBA_RE.match(value.strip())
    if m:
        r = int(m.group(1))
        g = int(m.group(2))
        b = int(m.group(3))
        a_raw = m.group(4)
        if "." in a_raw:
            a = int(float(a_raw) * 255)
        else:
            n = int(a_raw)
            a = int(n * 255) if n <= 1 else n
        a = max(0, min(255, a))
        return QColor(r, g, b, a)

    return QColor(value)


def build_stylesheet() -> str:
    t = config.ui.theme
    f = t.fonts
    s = t.surfaces
    b = t.borders
    tx = t.text
    a = t.accent
    se = t.semantic
    L = config.ui.layout

    return f"""
    QWidget {{
        background-color: {s.l0};
        color: {tx.primary};
        font-family: "{f.ui}", "Segoe UI", sans-serif;
        font-size: {f.size_md}px;
    }}

    QMainWindow {{ background-color: {s.l0}; }}
    QLabel {{ background: transparent; }}

    QLabel#PanelTitle {{
        color: {tx.primary};
        font-size: {f.size_md}px;
        font-weight: 600;
        background: transparent;
        padding: 0px;
    }}

    /* ─── SIDEBAR ────────────────────────────────────────── */
    QWidget#Sidebar {{
        background-color: {s.l1};
        border-right: 1px solid {b.subtle};
    }}
    QFrame#SidebarButton {{
        background: transparent;
        border: none;
        border-radius: {L.panel_radius}px;
        margin: 2px 8px;
    }}
    QFrame#SidebarButton[hover="true"] {{ background-color: {s.l3}; }}
    QFrame#SidebarButton[active="true"] {{
        background-color: {a.subtle};
        border-left: 3px solid {a.main};
    }}
    QLabel#SidebarIcon {{ color: {tx.secondary}; font-size: 16px; background: transparent; }}
    QLabel#SidebarLabel {{ color: {tx.secondary}; font-size: {f.size_lg}px; font-weight: 500; background: transparent; }}
    QFrame#SidebarButton[active="true"] QLabel#SidebarIcon,
    QFrame#SidebarButton[active="true"] QLabel#SidebarLabel {{ color: {a.main}; }}
    QLabel#SidebarBrandIcon {{ color: {a.main}; font-size: 22px; font-weight: 700; background: transparent; }}
    QLabel#SidebarBrandText {{ color: {tx.primary}; font-size: {f.size_xl}px; font-weight: 700; background: transparent; }}
    QFrame#SidebarDivider {{ background-color: {b.subtle}; border: none; }}

    /* ─── RIBBON ─────────────────────────────────────────── */
    QWidget#Ribbon {{
        background-color: {s.l1};
        border-bottom: 1px solid {b.subtle};
    }}
    QLabel#RibbonPageTitle {{ color: {tx.primary}; font-size: {f.size_lg}px; font-weight: 600; background: transparent; }}
    QLabel#RibbonContext {{ color: {tx.muted}; font-size: {f.size_md}px; background: transparent; }}

    /* ─── BUTTONS ────────────────────────────────────────── */
    QPushButton {{
        background-color: rgba(255, 255, 255, 0.05);
        color: {tx.primary};
        border: 1px solid {b.normal};
        padding: 6px 14px;
        border-radius: 8px;
        font-size: {f.size_md}px;
        font-weight: 600;
    }}
    QPushButton:hover {{ background-color: rgba(255, 255, 255, 0.10); border: 1px solid {b.strong}; }}
    QPushButton:pressed {{ background-color: rgba(255, 255, 255, 0.02); }}
    
    QPushButton#Ghost {{ background: transparent; border: 1px solid {b.subtle}; color: {tx.secondary}; }}
    QPushButton#Ghost:hover {{ background-color: {s.l3}; color: {tx.primary}; }}

    QPushButton#Primary {{
        background-color: {a.main};
        border: 1px solid {a.active};
        color: #000000;
        font-weight: 700;
        text-transform: uppercase;
        border-radius: 17px;
    }}
    QPushButton#Primary:hover {{ background-color: {a.hover}; }}
    QPushButton#Primary:pressed {{ background-color: {a.active}; }}
    QPushButton#Primary[mode="stop"] {{
        background-color: {se.down};
        border: 1px solid {se.down_active};
        color: #ffffff;
    }}
    QPushButton#Primary[mode="stop"]:hover {{ background-color: {se.down_hover}; }}

    /* ─── INPUTS (Arrow drawing handled in custom.py) ────── */
    QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
        background-color: {s.l2};
        color: {tx.primary};
        border: 1px solid {b.normal};
        border-radius: {L.panel_radius}px;
        padding: 6px 12px;
        font-size: {f.size_md}px;
        font-family: "{f.mono}", monospace;
    }}
    QComboBox:hover, QLineEdit:hover {{ border: 1px solid {b.strong}; background-color: {s.l3}; }}
    QComboBox:focus, QLineEdit:focus {{ border: 1px solid {a.main}; }}
    
    /* Reserve right padding for our custom-drawn arrow */
    QComboBox {{ padding-right: 28px; }}
    QDoubleSpinBox {{ padding-right: 24px; }}
    
    QComboBox QAbstractItemView {{
        background-color: {s.l2};
        color: {tx.primary};
        border: 1px solid {b.normal};
        border-radius: {L.panel_radius}px;
        padding: 4px;
        selection-background-color: {a.subtle};
        selection-color: {a.main};
    }}

    /* ─── TABLES ─────────────────────────────────────────── */
    QTableView {{
        background-color: transparent;
        border: none;
        gridline-color: transparent;
        selection-background-color: {a.subtle};
        selection-color: {tx.primary};
        font-family: "{f.mono}", monospace;
        font-size: {f.size_sm}px;
        outline: none;
    }}
    QTableView::item {{ padding: 4px 6px; border: none; }}
    QHeaderView {{ background-color: transparent; }}
    QHeaderView::section {{
        background-color: transparent;
        color: {tx.muted};
        border: none;
        border-bottom: 1px solid {b.subtle};
        padding: 8px 6px;
        font-size: {f.size_sm}px;
        font-weight: 600;
        letter-spacing: 0.5px;
        text-transform: uppercase;
        font-family: "{f.ui}", sans-serif;
    }}

    /* ─── SLEEK SCROLL BARS ──────────────────────────────── */
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        margin: 0px;
        border: none;
    }}
    QScrollBar::handle:vertical {{
        background-color: {b.strong};
        border-radius: 4px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background-color: {tx.secondary};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
        background: none;
        border: none;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}
    QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical {{
        image: none;
        width: 0px;
        height: 0px;
        border: none;
    }}

    QScrollBar:horizontal {{
        background: transparent;
        height: 8px;
        margin: 0px;
        border: none;
    }}
    QScrollBar::handle:horizontal {{
        background-color: {b.strong};
        border-radius: 4px;
        min-width: 30px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background-color: {tx.secondary};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
        background: none;
        border: none;
    }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        background: none;
    }}
    QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal {{
        image: none;
        width: 0px;
        height: 0px;
        border: none;
    }}

    /* ─── MENUS ──────────────────────────────────────────── */
    QMenu {{ background-color: {s.l2}; color: {tx.primary}; border: 1px solid {b.normal}; border-radius: {L.panel_radius}px; padding: 6px; }}
    QMenu::item {{ padding: 8px 24px 8px 16px; border-radius: 6px; font-size: {f.size_md}px; }}
    QMenu::item:selected {{ background-color: {a.subtle}; color: {a.main}; }}
    QMenu::separator {{ height: 1px; background-color: {b.subtle}; margin: 4px 8px; }}
    
    QPlainTextEdit {{ background-color: transparent; color: {tx.secondary}; border: none; font-family: "{f.mono}", monospace; font-size: {f.size_sm}px; padding: 8px; }}
    QDialog, QMessageBox {{ background-color: {s.l1}; }}
    QMessageBox QLabel {{ color: {tx.primary}; }}
    """