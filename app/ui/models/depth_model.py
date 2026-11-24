from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

from app.core.config import config


class DepthModel(QAbstractTableModel):
    """Cumulative market depth view.

    Top half: asks (highest price at top, best ask just above mid).
    Bottom half: bids (best bid just below mid, descending).
    Each row shows [Price, Qty at level, Cumulative qty from best
    price to this level]. The cumulative column is what makes this
    different from the order book: it reveals where liquidity walls
    are, not just what's at the top of book.
    """

    COL_PRICE = 0
    COL_QTY = 1
    COL_CUM = 2

    def __init__(self, widget_id: str = "depth") -> None:
        super().__init__()

        widget_cfg = config.ui.widgets.get(widget_id)
        cfg: dict = widget_cfg.model_dump() if widget_cfg is not None else {}

        self.levels: int = int(cfg.get("levels_to_show", 15))
        self.price_dp: int = int(cfg.get("price_decimals", 2))
        self.qty_dp: int = int(cfg.get("qty_decimals", 4))
        self.show_totals: bool = bool(cfg.get("show_totals_row", True))

        # Each entry: (price, qty, cumulative_qty)
        self._bids: list[tuple[float, float, float]] = []
        self._asks: list[tuple[float, float, float]] = []

        self._total_bid: float = 0.0
        self._total_ask: float = 0.0
        self._max_cum: float = 1.0

    # ─── Qt table model API ─────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        base = self.levels * 2
        # One extra row for the "spread / totals" divider when enabled.
        return base + (1 if self.show_totals else 0)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return 3

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.DisplayRole,
    ):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return ["Price", "Qty", "Cum"]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None

        r = index.row()
        c = index.column()
        theme = config.ui.theme

        # ── Totals / spread divider row ─────────────────────
        if self.show_totals and r == self.levels:
            if role == Qt.DisplayRole:
                if c == self.COL_PRICE:
                    return "— totals —"
                if c == self.COL_QTY:
                    return f"{self._total_bid:.{self.qty_dp}f}"
                return f"{self._total_ask:.{self.qty_dp}f}"
            if role == Qt.ForegroundRole:
                return QColor(theme.text.muted)   # was theme.text_muted
            if role == Qt.TextAlignmentRole:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if role == Qt.FontRole:
                f = QFont()
                f.setItalic(True)
                return f
            return None

        # ── Translate row → (price, qty, cum, side) ─────────
        if self.show_totals:
            # Rows above the divider (0..levels-1) are asks;
            # rows below (levels+1..) are bids.
            if r < self.levels:
                idx = self.levels - 1 - r
                row = self._asks[idx] if idx < len(self._asks) else None
                side = "ask"
            else:
                idx = r - self.levels - 1
                row = self._bids[idx] if idx < len(self._bids) else None
                side = "bid"
        else:
            if r < self.levels:
                idx = self.levels - 1 - r
                row = self._asks[idx] if idx < len(self._asks) else None
                side = "ask"
            else:
                idx = r - self.levels
                row = self._bids[idx] if idx < len(self._bids) else None
                side = "bid"

        if row is None:
            return None

        price, qty, cum = row

        if role == Qt.DisplayRole:
            if c == self.COL_PRICE:
                return f"{price:.{self.price_dp}f}"
            if c == self.COL_QTY:
                return f"{qty:.{self.qty_dp}f}"
            return f"{cum:.{self.qty_dp}f}"

        if role == Qt.ForegroundRole:
            return QColor(
                theme.semantic.down if side == "ask" else theme.semantic.up
            )

        if role == Qt.BackgroundRole and c == self.COL_CUM:
            base = QColor(
                theme.semantic.down if side == "ask" else theme.semantic.up
            )
            ratio = cum / self._max_cum if self._max_cum > 0 else 0.0
            ratio = max(0.0, min(1.0, ratio))
            base.setAlpha(int(30 + ratio * 110))
            return base

        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignRight | Qt.AlignVCenter)

        return None

    # ─── Public update ──────────────────────────────────────

    def update_book(
        self,
        bids: list[tuple[float, float]] | list[list[float]],
        asks: list[tuple[float, float]] | list[list[float]],
    ) -> None:
        raw_bids = [(float(p), float(q)) for p, q in bids[: self.levels]]
        raw_asks = [(float(p), float(q)) for p, q in asks[: self.levels]]

        # Cumulative sums from best price outward
        self._bids = []
        cum = 0.0
        for p, q in raw_bids:
            cum += q
            self._bids.append((p, q, cum))
        self._total_bid = cum

        self._asks = []
        cum = 0.0
        for p, q in raw_asks:
            cum += q
            self._asks.append((p, q, cum))
        self._total_ask = cum

        self._max_cum = max(self._total_bid, self._total_ask, 1e-9)

        if self.rowCount() == 0:
            return

        top_left = self.index(0, 0)
        bottom_right = self.index(self.rowCount() - 1, self.columnCount() - 1)
        self.dataChanged.emit(
            top_left,
            bottom_right,
            [Qt.DisplayRole, Qt.ForegroundRole, Qt.BackgroundRole, Qt.FontRole],
        )