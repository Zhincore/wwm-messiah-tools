# Own Lua scripts in the game client

`tools/wwm/scripts.py` (`wwm-scripts`) installs a small loader and manages the Lua files it runs. Client-side
only; it modifies the install, so only when the user asks, game closed.

```
wwm-scripts <game_dir> status
wwm-scripts <game_dir> install                     # loader + boot line in the cached hotfixes
wwm-scripts <game_dir> add FILE.lua ...            # copy in and enable
wwm-scripts <game_dir> disable|enable|remove NAME
wwm-scripts <game_dir> uninstall                   # hotfix rows back to the journalled originals
```
`<game_dir>` is the install folder that holds `LocalData`.

## How it gets in
- The game caches server hotfixes (Lua code strings) in `LocalData/LocalDB/db_mp`, table `localdb`, keys
  `hotfix_*` (msgpack, XOR with the NetEase key), and runs them at every start without re-checking them.
- `install` prepends one line to each: `pcall(function() load(MPatch.GetRawDataInSystem("LocalData","userscripts/loader.lua"))() end)`.
  The original row contents go to `LocalData/userscripts/journal.json`.
- The cache exists only after one login. The server can replace hotfixes at any time; a replaced row loses
  the line. `status` then shows fewer patched rows than cached ones: run `install` again (it adds the
  new originals to the journal). `uninstall` restores only rows that still carry the line.

## Files in `LocalData/userscripts/`
| File | Role |
|---|---|
| `loader.lua` | runs once per cached hotfix; runs each enabled script once per session |
| `enabled.txt` | one script name per line, run order |
| `<name>.lua` | a script |
| `scripts.log` | loader and script log, emptied at every game start |
| `journal.json` | original hotfix rows, for `uninstall` |

## Writing a script
A script is a Lua chunk called once per session, early (title screen: `G.main_player` does not exist yet),
with one argument:
```lua
local ctx = ...
ctx.log("x = %d", 1)            -- line in scripts.log
ctx.pcall(fn, ...)              -- guarded call: results, or nil after logging the error
ctx.timer(seconds, fn)          -- once, guarded
ctx.every(seconds, fn)          -- repeatedly for the rest of the session, guarded
```
- Never let an error escape into the game: an unguarded error in a callback reaches the game's crash
  reporter, which uploads it. Hand the game only callbacks that go through `ctx.pcall`.
- Wait for what you need: poll with `ctx.every` and return early until `G.main_player` etc. exist.
- The VM is not stock Lua 5.4: game APIs want `dict()` / `list()` values, not plain tables; data table
  rows (`G.datam.<table>`) are read-only.
- Changing a script file needs a game restart (a script runs once per session).

## Checked / not checked
- Checked (2026-11-02/r64785, Linux/Proton): install → uninstall leaves every `localdb` row byte-identical;
  the loader with one script in a running client (triggered by evaluating the boot line, not by a
  hotfix start); the same hotfix boot line technique starts the dockerized-wwm scripts.
- Not checked: a real game start running the patched hotfixes with this loader; whether the anti-cheat
  or the server notices any of this.
