-- Script loader, installed as LocalData/userscripts/loader.lua by `wwm-scripts install`.
-- The game runs it once per cached hotfix at startup; each script listed in userscripts/enabled.txt is
-- run once per session and called with the `ctx` helpers below as its only argument.
-- Nothing here may raise: an error that escapes reaches the game's crash reporter.

local DIR = "userscripts"
local S = _G.__userscripts or { loaded = {} }
_G.__userscripts = S

local function read(name)
  local ok, data = pcall(MPatch.GetRawDataInSystem, "LocalData", DIR .. "/" .. name)
  if ok and type(data) == "string" and #data > 0 then return data end
end

local function log(fmt, ...)
  local ok, line = pcall(string.format, fmt, ...)
  if not ok then line = tostring(fmt) end
  local path = MEngine.GetFileSystemBasePath("LocalData") .. "/" .. DIR .. "/scripts.log"
  local f = io.open(path, S.logged and "a" or "w")   -- the log starts empty every session
  S.logged = true
  if f then f:write(os.date("%H:%M:%S ") .. line .. "\n"); f:close() end
end

local ctx = { log = log }

-- fn(...) guarded: its results, or nil after logging the error.
function ctx.pcall(fn, ...)
  local r = { pcall(fn, ...) }
  if not r[1] then log("error: %s", tostring(r[2])); return nil end
  return table.unpack(r, 2)
end

-- Run fn once after `delay` seconds.
function ctx.timer(delay, fn)
  pcall(MEngine.AddCallback, delay, function() ctx.pcall(fn) end)
end

-- Run fn every `period` seconds for the rest of the session.
function ctx.every(period, fn)
  local function tick()
    ctx.pcall(fn)
    ctx.timer(period, tick)
  end
  ctx.timer(period, tick)
end

for name in (read("enabled.txt") or ""):gmatch("[^\r\n]+") do
  if not S.loaded[name] then
    S.loaded[name] = true
    local src = read(name .. ".lua")
    local fn, err
    if src then fn, err = load(src, "@" .. DIR .. "/" .. name .. ".lua") else err = "file missing or empty" end
    if fn then
      local ok, e = pcall(fn, ctx)
      log("%s: %s", name, ok and "loaded" or ("failed: " .. tostring(e)))
    else
      log("%s: not loaded: %s", name, tostring(err))
    end
  end
end
