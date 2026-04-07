"""
fix_tiers.py — Run once to correct BOSCHLTD and HCLTECH tiers
after fresh data fetch changed their G/C ratios.
"""
import json, sys, os
sys.path.insert(0, '.')

CACHE_PATH = 'data/tier_cache.json'
cache = json.load(open(CACHE_PATH))

TIER_LABELS = {
    1: 'Tier 1 — Strong trend',
    2: 'Tier 2 — Moderate',
    3: 'Tier 3 — Skipped (choppy)',
}

fixes = [
    # (symbol,  new_tier, new_gc,   new_optimal_cd, reason)
    ('BOSCHLTD', 3, 0.006, 999, 'G/C dropped 1.798→0.006 with fresh data'),
    ('HCLTECH',  1, 2.446,  50, 'G/C upgraded 1.984→2.446 with fresh data'),
]

print('Applying tier fixes:\n')
for sym, tier, gc, cd, reason in fixes:
    old_tier = cache[sym]['tier']
    cache[sym]['tier']             = tier
    cache[sym]['gc_ratio']         = gc
    cache[sym]['label']            = TIER_LABELS[tier]
    cache[sym]['optimal_cooldown'] = cd
    print(f'  {sym:<14} T{old_tier} → T{tier}  ({reason})')

json.dump(cache, open(CACHE_PATH, 'w'), indent=2)
print('\n✅ Cache saved.')

# Final summary
t1 = [(s, v['gc_ratio'], v.get('optimal_cooldown','?')) 
      for s,v in cache.items() if v['tier']==1]
t2 = [(s, v['gc_ratio'], v.get('optimal_cooldown','?')) 
      for s,v in cache.items() if v['tier']==2]
t3 = [s for s,v in cache.items() if v['tier']==3]

print(f'\n{"="*55}')
print(f'FINAL TIER BREAKDOWN')
print(f'{"="*55}')
print(f'\nTier 1 — risk=0.15  (strong trend):')
for s, gc, cd in sorted(t1):
    print(f'  {s:<14} G/C={gc:.3f}x  cd={cd}')
print(f'\nTier 2 — risk=0.10  (moderate):')
for s, gc, cd in sorted(t2):
    print(f'  {s:<14} G/C={gc:.3f}x  cd={cd}')
print(f'\nTier 3 — SKIPPED ({len(t3)} stocks):')
print(f'  {", ".join(sorted(t3))}')