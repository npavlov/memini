-- inventory.lua: what a player carries.
local Inventory = {}
Inventory.__index = Inventory

local MAX_ITEMS = 40

local function clamp(value, floor, ceiling)
  if value < floor then
    return floor
  end
  return math.min(value, ceiling)
end

-- A new, empty inventory.
function Inventory.new(owner)
  local self = setmetatable({}, Inventory)
  self.owner = owner
  self.items = {}
  return self
end

function Inventory:add(name, count)
  local current = self.items[name] or 0
  self.items[name] = clamp(current + count, 0, MAX_ITEMS)
end

function Inventory:count(name)
  return self.items[name] or 0
end

return Inventory
