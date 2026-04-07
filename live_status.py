# live_status.py
# Singleton that holds the current live feed state.
# Both the WebSocket push loop and the REST /live_status route read from here.
# Avoids passing the runner object around Flask routes.


class LiveStatus:
    def __init__(self):
        self._connected   = False
        self._instrument  = ""
        self._ltp         = None
        self._instruments = []
        self._portfolio_equity = 0.0

    def update(self, connected: bool, instrument: str = "",
               ltp: float = None, instruments: list = None,
               portfolio_equity: float = 0.0):
        self._connected        = connected
        self._instrument       = instrument
        self._ltp              = ltp
        self._instruments      = instruments or []
        self._portfolio_equity = portfolio_equity

    def get_status(self) -> dict:
        return {
            "connected":        self._connected,
            "instrument":       self._instrument,
            "ltp":              self._ltp,
            "instruments":      self._instruments,
            "portfolio_equity": self._portfolio_equity,
        }


# Module-level singleton — import this everywhere
live_status = LiveStatus()
