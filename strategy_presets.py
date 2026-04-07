# strategy_presets.py
#
# CUSTOM MODE ONLY — Conservative/Balanced/Aggressive presets removed.
# The user configures risk, RR, cooldown, min_price_distance via the UI.
# One locked config stored in DB for live engine.

import db

# Single default — used when nothing is locked yet
DEFAULT_CONFIG = {
    "name":               "Custom",
    "risk_per_trade":     0.01,
    "rr_target":          3,
    "cooldown":           20,
    "min_price_distance": 5,
}

# Empty — no preset buttons shown in UI
PRESETS    = []
SLOT_NAMES = ["Config A", "Config B", "Config C"]


# ── Saved config slots ────────────────────────────────────────────────────────

def _load_slots() -> dict:
    return db.get("saved_configs", default={})


def get_all_slots() -> list:
    saved = _load_slots()
    return [
        saved[name] if name in saved else {"name": name, "empty": True}
        for name in SLOT_NAMES
    ]


def save_to_slot(slot_name: str, config: dict) -> dict:
    if slot_name not in SLOT_NAMES:
        raise ValueError(f"Invalid slot '{slot_name}'. Must be one of {SLOT_NAMES}")
    slots = _load_slots()
    config["name"] = slot_name
    config.pop("empty", None)
    slots[slot_name] = config
    db.set("saved_configs", slots)
    return config


def load_slot(slot_name: str) -> dict:
    slots = _load_slots()
    if slot_name not in slots:
        raise ValueError(f"Slot '{slot_name}' is empty.")
    return slots[slot_name]


# ── Locked live config ────────────────────────────────────────────────────────

def lock_config(config: dict) -> dict:
    """Lock a config as the active live engine config. Stored in DB."""
    config.setdefault("name", "Custom")
    db.set("locked_config", config)
    print(f"✅ Config '{config['name']}' locked for live trading.")
    return config


def load_locked_config() -> dict:
    """Load the currently locked live config. Falls back to DEFAULT_CONFIG."""
    return db.get("locked_config", default=DEFAULT_CONFIG)


# Alias for multi_strategy_runner
def load_locked_preset() -> dict:
    return load_locked_config()