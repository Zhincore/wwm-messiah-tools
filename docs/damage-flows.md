# Damage and heal flows

The maths of a hit is not in the `.ft` formula files but in generated Lua: `hexm/common/formula_gen/code/*.lua`
in the decompiled scripts (96 files, 49 flows + `_grey` variants). `wwm-data flows <lua_dump> -o OUT` writes
them as plain Lua (wrapper and `_ENV.` prefixes removed). They run in `hexm/common/combat/damage_manager.lua`
(`get_calc_mod`, environment `CALC_ENV`); the result is `FLOW_RESULT`.

Names in a flow: `AA.x` attacker attribute, `BB.x` target attribute (both final values from the formula
files), `SKCALC.x` / `SKILL.x` the skill's numbers, `uniform(a, b)` a random number, `config(table, field,
key)` a data-table cell, `rtable(...)` a weighted outcome pick (higher priority takes its share first).

## Flows
| Flow | What |
|---|---|
| `ZD_QX_DMG` | normal HP damage of a hit (below) |
| `ZD_QX_DMG_GW` | the same against monsters |
| `ZD_QX_HEAL` | healing |
| `ZD_DX_DMG`, `ZD_ZQ_DMG`, `ZD_VIT_DMG`, `ZD_TP_DMG` | other damage kinds of a hit (not read in detail) |
| `BFB_QX_DMG`, `BFB_QX_HEAL` | percentage-based damage / heal |
| `W_ATK_PEN_SHOW`, `PRO_[A-E]_PEN_SHOW` | the penetration bonus shown in the UI |
| `PET_*`, `CIRCKET_*`, `HS_*`, `YS_*`, `LIFE_*`, `SLAP_*`, `KFC_DMG`, `cs_*` | pets, cricket fights, careers, environment damage, mini-games |

## `ZD_QX_DMG` in short
Read from the flow; variable names are the game's.

**1. Outcome.** `C = BB.JUDGE_ATTR_CORRECT_PARA` (the target's world-level factor).
```
precision = clamp((AA.ACR_PROB - 0.65) / (1 + C) + 0.65 + AA.ADD_ACR_PROB - BB.ADD_ACR_RESIST_PROB, 5%, 100%)
crit      = min(max(AA.CRI_PROB  / (1 + C) + AA.ADD_CRI_PROB  - BB.ADD_CRI_RESIST_PROB,  0), 80%) + AA.DIRECT_CRI_PROB
affinity  = min(max(AA.BASH_PROB / (1 + C) + AA.ADD_BASH_PROB - BB.ADD_BASH_RESIST_PROB, 0), 40%) + AA.DIRECT_BASH_PROB

precision roll passes: pick affinity first, then crit from what is left, else an ordinary hit
precision roll fails:  affinity roll alone; if that fails too the hit is an abrasion, unless
                       ABR_CORRECT_PRO + SKCALC.SKILL_ABR_CORRECT + AA.ABR_AVOID_PROB rolls it back to ordinary
```
A skill can force an outcome (`MUST_CRI_DMG_PRO`, `MUST_BASH_DMG_PRO`, …) before any of this.

**2. Attack value used.** Ordinary and critical: `uniform(MIN_W_ATK, MAX_W_ATK)`. Affinity: `MAX_W_ATK`.
Abrasion: `MIN_W_ATK`. The same for each of the five element attacks (`PRO_ATK_A..E`).

**3. Physical part.**
```
signal = BB.W_ATK_PEN_RDC - AA.W_ATK_PEN + (BB.CORRECTABLE_W_ATK_PEN_RDC - AA.CORRECTABLE_W_ATK_PEN) / BB.ATK_PEN_CORRECT_PARA
pen    = -signal / 200                      if signal < 0      (more penetration than resistance: +0.5% per point)
       = -signal / 100                      if 0 <= signal <= 75
       = -signal*4 / (100 + signal*4)       if signal > 75     (diminishing)
physical = (max(atk * (1 + SKILL_RATIO_W_ATK) - BB.W_DEF * (1 - max((AA.W_ATK_IGR - BB.W_ATK_IGR_RDC) / 1000, 0)), AA.MIN_W_ATK / 10)
            * SKILL_POWER_W_ATK + SKILL_ADD_W_ATK * (1 + SKILL_ADD_ATK_UP_RATIO))
           * (1 + pen) * max(1 + AA.W_ATK_SCALE - BB.W_DEF_SCALE, 0)
```
Defense is subtracted from the attack value, with a floor of a tenth of Min Physical Attack.

**4. Element parts** (A..E, summed): no defense subtraction; resistance acts through the same `pen` curve with
`signal = BB.PRO_DEF_x - AA.PRO_PEN_x - AA.CORRECTABLE_PRO_PEN_x / BB.ATK_PEN_CORRECT_PARA`, and each part is
scaled by the martial art's weight `config("kongfu", "WX_PRO_ATK_x", kongfu id)`.

**5. Outcome multiplier.**
```
critical: clamp(1 + AA.W_ATK_CRI_UP - BB.W_ATK_CRI_UP_RDC + buffs (+ a per-hit-count term), 1.35, 2.5)
affinity: clamp(1 + AA.BASH_UP - BB.BASH_UP_RDC + buffs, 1.2, 2.3)
abrasion: AA.CS_ITS
```

**6. Final.**
```
ceil(max(((physical + elements + bow bonus) * outcome multiplier
     * (play_cause_up_rate + (weapon + skill + monster type boosts) / BB.SHENLI_ATTR_CORRECT_PARA + AA.DAMAGE_UP)
     * (1 + SPECIFIC_SKILL_EFFECT_UP / BB.SKILL_EFFECT_CORRECT_PARA)
     * (1 + play_separate_up_rate + weak-state bonus)
     * max(1 - DAMAGE_REDUCTION_BY_TALENT / 100, 0.01) * play_cause_re_rate
     * (1 - MONSTER_TYPE_DMG_RE) * (1 - BB.DAMAGE_REDUCE) + play_cause_change_value)
     * SKILL.jm_numerial_ratio * (1 + TARGET_TAG_CORRECT), 0))
```

## `ZD_QX_HEAL` in short
Heal crit chance is the crit formula without the target's resistance. Base is the mean of Min and Max attack
× `uniform(0.9, 1.1)` (physical and Silkbind parts), × skill power, × `(1 + penetration / 200)`,
× `(1 + heal scale)`; a critical heal multiplies by `1 + AA.HEAL_CRI_UP`.

## Open
- Where the skill numbers (`SKCALC.*`) and the `play_cause_*` / `*_TYPE_DMG_UP` terms are filled (`damage_manager.lua`).
- The other `ZD_*` flows.
