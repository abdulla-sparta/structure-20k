#!/usr/bin/env python3
"""
patch_cache_cooldowns.py
Run once after classification to store optimal cooldowns in tier_cache.json
"""
import json, os, sys
sys.path.insert(0, '.')

CACHE_PATH = 'data/tier_cache.json'
if not os.path.exists(CACHE_PATH):
    print("ERROR: data/tier_cache.json not found. Run classification first.")
    sys.exit(1)

cache = json.load(open(CACHE_PATH))
print(f"Cache loaded: {len(cache)} symbols\n")

# Per-stock optimal cooldowns (discovered via backtest sensitivity analysis)
OPTIMAL_COOLDOWNS = {
    # Tier 1 — strong trend, cd=20 best (more signals, all profitable)
    'TRENT':      20,
    'CUMMINSIND': 20,
    'M&M':        20,
    # Tier 2 — each stock has different sweet spot
    'OFSS':       75,   # +84% at cd=75
    'PERSISTENT': 20,   # +48% at cd=20
    'MPHASIS':    75,   # +28% at cd=75
    'BOSCHLTD':   20,   # consistent across all cooldowns
    'RELIANCE':   20,   # +22% at cd=20 (was -5.6% at cd=50!)
    'HCLTECH':    50,   # +32% at cd=50
}

updated = 0
for sym, cd in OPTIMAL_COOLDOWNS.items():
    if sym in cache:
        old_cd = cache[sym].get('optimal_cooldown', 'none')
        cache[sym]['optimal_cooldown'] = cd
        print(f"  {sym:<14} T{cache[sym]['tier']}  cooldown: {old_cd} → {cd}")
        updated += 1
    else:
        print(f"  WARNING: {sym} not in cache — run reclassification first")

json.dump(cache, open(CACHE_PATH, 'w'), indent=2)
print(f"\n✅ {updated} stocks updated. Saved to {CACHE_PATH}")

# Print full config table for verification
print("\n" + "="*65)
print("FULL CONFIG TABLE — what each stock will use in backtest/live")
print("="*65)
print(f"{'Symbol':<14} {'Tier':>5} {'G/C':>7} {'Cooldown':>9} {'Status'}")
print("-"*50)

TIER_RISK = {1: 0.15, 2: 0.10, 3: 0.0}

for sym in sorted(cache.keys()):
    v = cache[sym]
    tier = v['tier']
    gc   = v.get('gc_ratio', '?')
    cd   = v.get('optimal_cooldown', {1:20,2:50,3:999}[tier])
    risk = TIER_RISK[tier]
    gc_str = f"{gc:.3f}x" if isinstance(gc, float) else str(gc)
    
    if tier == 3:
        status = "SKIPPED (T3)"
    elif tier == 1:
        status = f"ACTIVE T1 — risk={risk}  cd={cd}"
    else:
        status = f"ACTIVE T2 — risk={risk}  cd={cd}"
    
    print(f"  {sym:<14} T{tier}  {gc_str:>7}  cd={cd:<6}  {status}")