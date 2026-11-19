# Gear rolls: tables and math

Which stats a gear item can roll, with what chance and range. Export: `tools/wwm/tuning.py`
(`wwm-tuning <hd_dir> -o OUT`). Roll logic is from the decompiled module the client shares with the
server, `hexm/common/misc/equip_attr_misc.lua` (+ `nmath.rand_choice_with_weights_*`); the server runs
its own copy, so treat the logic as "what the client believes". Checked against a running client: the
exported weights and ranges equal `G.datam.*` (2026-11-02/r64785).

## Source tables → output files

| File | Table | Key | Content |
|---|---|---|---|
| `affixes.json` | `equip_affix_config` (+ `formula_base_attrs`) | affix id | one rollable stat; `name`, `attr` added |
| `pools.json` | `equip_toning_group` | group id | `{affix id: weight}` |
| `attunements.json` | `equip_determin_group` | attunement type id | pool, kind, path, costs |
| `items.json` | `equip_items` (+ `equip_attr_cols`) | item id | which pools an item uses, base attributes, set odds |
| `levels.json` | `equip_toning_mode` | tuning mode (`items.re_cost`) | exp and coin cost per tuning level |
| `materials.json` | `equip_toning_props`, `tone_critical_props`, `toning_prop_config`, `stuff_items` | | exp materials, floor stones, per-tier costs, item names |
| `legacy.json` | `equip_legacy` | gear level | inheritance ("legacy") growth and swap costs |
| `config.json` | `equip_config[1]` | | global constants (default quality tiers, limits) |
| `paths.json` | `neigong_liupai_data` | path id | path names |
| `sets.json` | `suit_affix_data` | set id | set names and piece counts |

### Affix fields (`affixes.json`)
| Field | Meaning |
|---|---|
| `random_group` | the pool this affix belongs to |
| `random_weight` | its weight (same number as in `pools.json`) |
| `min_val`, `max_val` | roll range (`value_range` when present overrides both for display maths) |
| `random_accuracy` | step; values are rounded down to a multiple of it |
| `quality_range` | three thresholds on `(v - min) / (max - min)` for the quality tiers; default `config.affix_quality_range` |
| `se_group`, `se_num` | family id and how many affixes of that family one item may hold |
| `permitted_equip_type` | item `subtype` / `equip_sub_type` values allowed to roll it; absent = any |
| `liupai`, `zhuanlv_liupai` | paths (`paths.json`) for path-filtered rolls; `zhuanlv_liupai` wins when present; `-1` = every path |
| `umlaut_prohibit_affix` | affixes removed from a re-attunement pool while this one is held |
| `affix_para` | attribute number (`formula_base_attrs`); `-1` = passive effect (`passive_skill`) |
| `value_show_form` | display format of the value |

Not in the client tables (the code reads them, so they are server-only): `random` (the value buckets
`[low, high, weight]`) and `det_random_improve`.

### Item fields (`items.json`)
| Field | Meaning |
|---|---|
| `initia_group` | pool of the stats an item drops with |
| `re_initial_num` | `[[count, weight]]`: how many stats it drops with |
| `re_group` | pool for tuning |
| `re_cost` | tuning mode (`levels.json`) |
| `det_groups` | attunement types (`attunements.json`) the item accepts, by slot number |
| `base_attrs` | per attribute `[[low, high, weight]]` |
| `suffix` | `[[set id, weight]]` |
| `re_reset_cost` | `[item id, count]` to reset tuning |

## Math

Every weighted pick is `P(x) = weight(x) / sum(weights)`; weight 0 never rolls.

### Which stat (`toning_random_affix`)
```
pool = copy of the group {affix: weight}
remove affixes the item already holds
remove affixes whose family is full: held affixes of that se_group from this group >= se_num
remove affixes whose permitted_equip_type does not contain the item's subtype / equip_sub_type
remove affixes blocked by forbidden_keys
if a path is passed: remove affixes whose path list has neither that path nor -1
pick by weight
```
Stats an item drops with come from `initia_group`, tuned ones from `re_group`. The groups hold different
affix ids, so the same attribute can appear once from each.

### How high (`random_tone_affix_val`)
```
buckets = affix.random                        -- server-only
floor level f in 1..3:  keep buckets with low >= min + quality_range[f] * (max - min)
floor level 4:          return max
"greater than g":       cut buckets to values above g + accuracy
pick a bucket by weight
v = low + rand() * (high + accuracy - low), rounded down to a multiple of accuracy
```
Quality tier of a value: the highest index `i` with `(v - min) / (max - min) >= quality_range[i]`.

### Floor materials (`tone_critical_props`)
`c_val` is the floor level above. Gear used as material carries a floor too: an affix with
`affix_para` 356 gives its value as the level, 324 gives level 4. `toning_prop_config.material_return_pro`
`[[chance, level]]` is the chance a floor material is returned (`calc_critical_back_items`; conditions not traced).

### Tuning levels (`equip_toning_mode`)
Level `n` needs `exp` more experience (`acc_exp` in total) at `currency_cost` coins per point; `max_lv` levels.

### Attunement (`tone_determin_random_affix`, `tone_redetermin_affix`)
```
attune:     pool = union of the pools (det_group) of the chosen attunement types; pick by weight; fresh value
re-attune:  pool = det_group of the current type
            remove the current affix only when its value is at max
            remove the current affix's umlaut_prohibit_affix
            pick by weight
            same affix   -> value strictly above the current one ("greater than")
            other affix  -> fresh value
```
`type` 1 = PvE stat, 2 = arena effect. `cost` / `again_cost` are `[item id, count]`.

### Base attributes and set (`random_equip_base_attrs`, `random_equip_suit_affix`)
Per attribute: pick a `[low, high, weight]` entry by weight, then a uniform value in it (integer when both
ends are integers). Set: pick from `suffix` by weight.

### Several rolls
Rolls are without replacement (the pool is rebuilt after each), so simulate: roll, add to the held
affixes, rebuild the pool, repeat.

## Open
- Which server call passes a path to `toning_random_affix` (assumed: the path-targeted re-tune).
- Conditions of the floor-material refund.
- `attunements.name` / `describe` are text numbers that the locale tables do not resolve.
