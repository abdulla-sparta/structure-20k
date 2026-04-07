# engine/state_manager.py
#
# Live engine state stored in DB via db.py.
# NEVER called during backtest runs — TradeEngine guards this with _is_backtest.

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db


class StateManager:

    def __init__(self, symbol: str = ""):
        self.symbol = symbol or "_global"

    def save(self, state: dict):
        db.set_engine_state(self.symbol, state)

    def load(self) -> dict | None:
        return db.get_engine_state(self.symbol)

    def clear(self):
        db.clear_engine_state(self.symbol)
