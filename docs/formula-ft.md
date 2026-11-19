# Formula files (`formula/*.ft`)

Attribute formulas of the engine: `formula_player.ft`, `_player_grey`, `_npc`, `_pet`, `_disease`, `_ququ`,
`_zzq_npc`, loose files next to the HD tables. Reader: `tools/wwm/formula.py` (`wwm-data formulas`).

A file is a dependency graph, like a spreadsheet: input variables, and formulas built from them. Each node
also stores its value for the default inputs. That gives a complete self-check: evaluating every formula
of all seven files (build 2026-11-02/r64785, 5,114 formulas) reproduces every stored value.

## Layout
Little-endian. Header: `u32 1, u32 build time (unix), u32 names_at (24), u32 formulas_at, u32 inputs_at, u32 nodes_at`.

1. **Names** `names_at..formulas_at`: NUL-terminated strings, sorted. Everything else refers to them by index.
2. **Formulas** `formulas_at..inputs_at`: `u16 count`, then `count` × `(u16 name, u16 root node)`.
3. **Inputs** `inputs_at..nodes_at`: `u16 count`, then per input `u16 name, u16 variable node, u16 n,
   n × u16 node` — the nodes to recompute when the input changes.
4. **Nodes** `nodes_at..`: `u16 count`, then per node `u32 size` (bytes after this field), `u16 kind,
   u16 index` (sequential), `f32 stored value`, then u16 fields by kind:

| Kind (game's class) | Fields | Meaning |
|---|---|---|
| 1 variable (`NameExpr`) | `name, node` | named value; `node` = the formula's root node, `0xFFFF` = input |
| 2 chain (`BinopListExpr`) | `n, operand, (op, operand)…` (`n` = number of u16 that follow) | `a op b op c …` evaluated left to right |
| 3 negate (`UnaryopExpr`) | `1, operand` | `-x` |
| 4 constant (`NumberExpr`) | (the f32 is repeated) | number |
| 5 string (`StringExpr`) | `name` | string literal (argument of `config`) |
| 6 call (`FunctionExpr`) | `name, argc, args…` | function call |

Chain operators: `0 +`, `1 -`, `2 *`, `3 /`, `9 ==` (checked against all stored values), `7 >`, `8 >=`
(inferred: two uses, `FINAL_CRI_B_chance > _RANDOM`, `FINAL_CRI_A_chance >= _RANDOM`; the stored values
also fit `<`/`<=`). Codes 4-6 do not occur. No chain mixes `* /` after `+ -`, so precedence never matters;
grouping is done with nested chains. Comparisons give 1 or 0.

Functions: `min`, `max` (2+ arguments), `floor`, `clamp(x, low, high)`, `config(...)`. Division by zero
occurs with default inputs (the stored value is then NaN/inf).

## How the game uses it
Evaluation is native: `hexm/common/formula_gen/runtime/formula_manager.lua` hands the raw file to
`formula_tree.load_tree_from_data(tree, bytes)` (C++), and entities own a `formula_tree.formula_data`
(`formula_cpp.lua`: `get`, `update_leaf`, `update_leaves`). Lua only writes inputs and receives changed
outputs (`update_attr_multi`). Trees: player, npc, disease, ququ, pet (+ `player_grey`); `zzq_npc` is not
referenced from Lua.

The same trees exist as Lua tables for two of them (`hexm/client/data*/attr_formula_data/formula_ququ.lua`,
`formula_pet.lua`: `Targets`, `Leaves`, `Exprs = {id, dependents, {Class, ...}, default}`), used when the
native loader is absent; the node classes above are from there, and node counts per class match the
binary. The build strips the Lua form of player/npc/disease (`cook.lua` `FILTER_TABLE_NAMES`).
`formula_<tree>_defaults.lua` lists every name's default value.

Functions the game registers for formulas (`attr_formula.lua`): `min`, `max`, `exp`, `pow`, `ln`, `clamp`,
`get_row_data`, `random`, `rand_or_fix`, `floor`, `sqrt`, `abs`, `real_rtable`. The files use only
`config`, `min`, `max`, `clamp`, `floor`.

Inputs are written by Lua under fixed prefixes: `BUFF_` (buffs), `EQUIP_`, `WUYIN_`, `TALENT_`, `DRUG_`
(`attr_server_add_base.lua`), plus `level`, `kongfu_main_lv`, `kongfu_sub_lv`, `active_kongfu`, `_HP_INPUT`.
`B.<name>` holds values of the target entity. Damage and heal maths are separate, readable Lua
(`hexm/common/formula_gen/code/*.lua`, run by `combat/damage_manager.lua`).

## Semantics
- `config('table', 'field', key[, default])` reads `field` of the row with that key in an HD data table
  (`avatar_base_attrs` by level, `attr_first_level_trans`, `avatar_world_attrs`, `id_attr_trans_rate`, …).
  Checked: the nine lookups whose default key has a row (`attr_first_level_trans`, key 1) equal the table
  cells. With no such row the stored value is the 4th argument when present, else 1. Two table names
  (`work_level_inc_attr`, `guild_dianxiu_attr_data`) are not HD tables. `formula.table_config(store)` is this lookup.
- Player attributes are layered. For an attribute `X`:
  `STEADY_X = BASE_X + ATTR_X + EQUIP_X + XINFA_X + WUKU_X + WUXUE_X + WUYIN_X + WORLD_X + TALENT_X`,
  `FIXED_X` adds trial-mode switches (`IS_ATTR_TP`), `VARI_X` adds buffs
  (`BUFF_X + AREA_BUFF_X + FIXED_X * (BUFF_X_RATE + AREA_BUFF_X_RATE) + …`), `X = FIXED_X + VARI_X`.
  `ATTR_X` comes from the five body attributes (`CON_HP_X * STEADY_CON + …`).
- Rate caps live here, e.g. `EFFECTIVE_CRI_PROB = min(FIXED_CRI_PROB / (1 + FIXED_JUDGE_ATTR_CORRECT_PARA), 0.8)`.
- Variable names with a prefix and a dot (`B.`, `BB.`, `II.`) appear in combat formulas; presumably the
  other party of a hit. Not traced.

## Open
- Where `work_level_inc_attr` and `guild_dianxiu_attr_data` come from.
- Which source (gear, inner ways, …) fills which `EQUIP_*`/`XINFA_*`/… input.
