# Datasets (`wwm-data raw`)

Bulk export of HD tables by topic (`tools/wwm/datasets.py`, patterns in `DATASETS`). One JSON file per
table, `{row key: row}`, with the game's own field names and text ids resolved. Tables whose keys repeat
(`"multi": true` in the manifest) map each key to the list of its rows. `__grey` variants are skipped.

Counts are from build 2026-11-02/r64785; every table below decodes without errors.

| Dataset | Tables | Rows | Patterns |
|---|---|---|---|
| `gacha` | 30 | 1,201 | `^gacha_`, `^activity_gacha_`, `^reward_gacha_`, `lottery` |
| `rewards` | 22 | 205,627 | `^rewards?(_|$)` |
| `gear` | 46 | 13,173 | `^equip_`, `^suit_`, `^tone_`, `^toning_`, `^average_enhance` |
| `inner_ways` | 36 | 5,022 | `^xinfa`, `_xinfa`, `^neigong_` |
| `martial_arts` | 49 | 4,464 | `^kongfu`, `^jianghu_skill`, `^qishu` |
| `attributes` | 19 | 36,641 | `^formula_`, `attr_to_cn_name$`, `^combat_const_data$`, `^entity_attr$`, `^calcpoint$` |
| `guild` | 186 | 3,479 | `^club_` |
| `arena` | 98 | 10,335 | `^lunjian`, `^season_reward`, `^rank_`, `^identity_pvp` |
| `items` | 56 | 38,459 | `^stuff_`, `^compound_` |

Anything else: `wwm-data raw <hd_dir> -o OUT --tables REGEX`.

## What is known per dataset

Only `gear` has its logic traced (see `gear-tuning.md`; `wwm-data tuning` is the shaped export of that part).
The notes below are read off the rows, not from code, unless stated.

### gacha
- `gacha_pool_common` — one row per pool: name, currency, `legend` (draw count of the guaranteed top
  reward, 150 for the main pools), `epic_times` (10), `pseudo_random`.
- `gacha_pool_to_reward` — pool → reward ids; `gacha_reward_config` — per reward (several unnamed `#n` fields).
- `gacha_grow_probability` — pool → draw number → `[top, epic, normal]` weights out of 1000. The main pool
  starts growing at draw 111 (`[3, 30, 967]`) and reaches `[1000, 0, 0]` at draw 150.
- `gacha_bad_luck_compensate` — `fix_probability [1, 30, 969]` and draw counters; looks like the base odds
  and a bad-luck compensation rule.
- `gacha_period_pool_distribute` — per period and pool: `[[reward id, weight]]` by rarity.
- `reward_gacha_to_max_count` — reward gacha → draw cap.

### rewards
- `rewards` — reward id → `fixed_stuff [[item id, count, ?]]` and designer name (Chinese).
- `reward_no_group`, `reward_group_limit`, `reward_ref_stuffs`, `reward_fix_stuffs` — grouping and item
  references; `rewards_exp_mode`, `rewards_token_mode` — repeated keys (multi).
- How a reward id is rolled into items is not traced.

### inner_ways, martial_arts
- `xinfa` (151) with `xinfa_uprank_info` / `xinfa_uprank_attr` (rank costs and stats),
  `xinfa_passive_skills`; `neigong_liupai_data` = the paths.
- `kongfu` (292), `kongfu_up_rank`, `kongfu_numerical`; `jianghu_skills*` = mystic skills and their levels.

### attributes
- `formula_base_attrs` — 424 attributes: `prop_name_en` (key used everywhere), display name and format,
  conversion parameters. `formula_fight_calc_params`, `formula_damage_calc_config` — combat constants.
- `entity_attr` — per-entity attribute rows; `calcpoint` — purpose not checked.
- The formulas themselves are not tables: see "Formula files" in `hd-store.md`.

### guild
All `club_*` tables: skills and levels, buildings, events (`club_play_*`), Guild War
(`club_battle_*`, `club_combat_*`, season config), rewards, conditions.

### arena
`lunjian_grade_info` and friends (grades), `season_reward_*` (season rewards per mode), `rank_data` /
`rank_name_to_config` (leaderboards), `identity_pvp_*`.

### items
`stuff_items` (13,595: name, description, sources via `access_mode`), `stuff_access*` (where an item
comes from), `compound_recipe_*` (crafting).
