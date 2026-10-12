// SPDX-License-Identifier: GPL-2.0-or-later
// Live Quake additions to FrikBot. Uses its normal movement and routing.
.float lq_seen, lq_progress_time, lq_retry_time, lq_cooldown;
.entity lq_activator, lq_blocked_way, lq_escape_way;
.float lq_blocked_until, lq_escape_until;
.vector lq_water_origin;
.float lq_water_progress;
.float lq_visit_time;
.vector lq_motion_origin;
.float lq_motion_time, lq_manoeuvre_start, lq_manoeuvre_count;
.float lq_stuck_event, lq_stuck_since, lq_checkpoint_needed;
.entity lq_cells;
.float lq_cell_count, lq_cell_time, lq_jump_until;
.vector lq_region;
.float lq_empty_since, lq_exit_retry, lq_damage_dealt, lq_ambient_lit;
.entity lq_locked_door;
.float lq_door_since, lq_door_block_until;
.vector lq_door_origin;
.entity lq_exit_goal;
.float lq_exit_failures;
.float lq_exit_routing;
.float lq_assist_dry_routing;
.float lq_water_avoid_until;
.float lq_parked, lq_fight_time, lq_event_time, lq_spawn_time;
float lq_dm_bots, lq_rerelease;
string lq_dm_next;
void() LQ_DmTopUp;
float lq_intermission_until;
float(entity scot) target_onstack;

// Called only by exit-route expansion; ordinary FrikBot routes are unchanged.
float(entity from, entity destination) lq_exit_link_open =
{
    local entity door;
    if (!cvar("lq_e1m1_assist")) return TRUE;
    traceline(from.origin, destination.origin, TRUE, from);
    door = trace_ent;
    if (door.classname != "door") return TRUE;
    if (door.owner) door = door.owner;
    if (!door.message || !door.targetname) return TRUE;
    if (door.state == 0 || door.state == 2) return TRUE;
    return FALSE;
};

float(entity way) lq_dry_way =
{
    local vector below;
    below = way.origin - '0 0 24';
    if (pointcontents(way.origin) != -1 || pointcontents(below) != -1) return FALSE;
    if (way.target1 && pointcontents(way.target1.origin) == -1) return TRUE;
    if (way.target2 && pointcontents(way.target2.origin) == -1) return TRUE;
    if (way.target3 && pointcontents(way.target3.origin) == -1) return TRUE;
    if (way.target4 && pointcontents(way.target4.origin) == -1) return TRUE;
    return FALSE;
};

void() lq_recover =
{
    local entity way, best;
    local float distance, score, bestscore;
    local entity cell;
    local vector region;
    if (self.lq_parked) return;
    if (deathmatch && self.enemy.classname == "player" && self.enemy.health > 0 && fisible(self.enemy))
    { self.lq_fight_time = time; return; }
    region_x = floor(self.origin_x / 128);
    region_y = floor(self.origin_y / 128);
    region_z = floor(self.origin_z / 128);
    cell = self.lq_cells;
    while (cell && cell.lq_region != region) cell = cell.lq_cells;
    if (!cell && self.lq_cell_count < 256)
    {
        cell = spawn();
        cell.classname = "lq_visited_cell";
        cell.lq_region = region;
        cell.lq_cells = self.lq_cells;
        self.lq_cells = cell;
        self.lq_cell_count = self.lq_cell_count + 1;
        self.lq_cell_time = time;
    }
    if (self.waterlevel >= 2)
    {
        if (!self.lq_water_progress || vlen(self.origin - self.lq_water_origin) > 24)
        {
            self.lq_water_origin = self.origin;
            self.lq_water_progress = time;
        }
        if (time - self.lq_water_progress >= 6)
        {
            self.lq_progress_time = time - 9;
            self.lq_retry_time = 0;
            self.lq_water_progress = time;
        }
    }
    else self.lq_water_progress = 0;
    way = FindWayPoint(world);
    if (way && vlen(way.origin - self.origin) < 64)
    {
        way.lq_visit_time = time;
        if (!(way.lq_seen & ClientBitFlag(self.b_clientno)))
        {
            way.lq_seen = way.lq_seen | ClientBitFlag(self.b_clientno);
            self.lq_progress_time = time;
        }
    }
    if (!self.lq_progress_time) self.lq_progress_time = time;
    if ((time - self.lq_progress_time < 9 && time - self.lq_cell_time < 9) || time < self.lq_retry_time)
        return;
    self.lq_retry_time = time + 9;
    // Ban the failed goal and link for two minutes, including stacked goals.
    if (self.target1 && self.target1.classname != "trigger_changelevel" && self.target1.classname != "player") self.target1.lq_cooldown = time + 120;
    if (self.target2 && self.target2.classname != "trigger_changelevel" && self.target2.classname != "player") self.target2.lq_cooldown = time + 120;
    if (self.target3 && self.target3.classname != "trigger_changelevel" && self.target3.classname != "player") self.target3.lq_cooldown = time + 120;
    if (self.last_way && self.target1.classname == "waypoint")
    {
        self.last_way.lq_blocked_way = self.target1;
        self.last_way.lq_blocked_until = time + 120;
    }
    self.target1 = self.target2 = self.target3 = self.target4 = world;
    self.enemy = world;
    self.lq_activator = world;
    self.b_aiflags = 0;
    self.keys = 0;
    self.oldkeys = -1;
    self.movevect = '0 0 0';
    self.route_failed = 0;
    ClearMyRoute();
    if (route_table == self) { route_table = world; busy_waypoints = 0; }
    best = world;
    bestscore = -100000;
    way = way_head;
    while (way)
    {
        distance = vlen(way.origin - self.origin);
        // Pick outside the local 384-unit region; route through links rather
        // than restricting recovery to waypoints visible from this corner.
        if (distance > 384 && distance < 2000 && way.lq_cooldown <= time && lq_dry_way(way))
        {
            score = -distance;
            if (!(way.lq_seen & ClientBitFlag(self.b_clientno))) score = score + 2400;
            score = score - way.lq_visit_time;
            if (score > bestscore) { best = way; bestscore = score; }
        }
        way = way._next;
    }
    if (best)
    {
        target_add(best);
        bot_get_path(best, TRUE);
        dprint("Live Quake: routing outside the stalled region\n");
    }

};

entity(entity destination, float depth) lq_find_activator =
{
    local entity candidate, result, best;
    local float distance, bestdistance;
    if (depth > 4 || !destination.targetname) return world;
    bestdistance = 100000;
    candidate = find(world, target, destination.targetname);
    while (candidate)
    {
        result = world;
        if (candidate.classname == "func_button" || candidate.classname == "trigger_once" || candidate.classname == "trigger_multiple")
            if (candidate.solid && candidate.lq_cooldown <= time) result = candidate;
        if (!result && candidate.targetname) result = lq_find_activator(candidate, depth + 1);
        if (result)
        {
            distance = vlen(realorigin(result) - self.origin);
            if (distance < bestdistance) { best = result; bestdistance = distance; }
        }
        candidate = find(candidate, target, destination.targetname);
    }
    return best;
};

void() lq_linked_door =
{
    local entity door, activator, candidate;
    local vector start, finish;
    if (self.lq_activator)
    {
        if (self.lq_activator.frame || self.lq_activator.nextthink > time)
            self.lq_activator = world;
        else
            return;
    }
    start = self.origin + '0 0 16';
    if (self.target1) finish = realorigin(self.target1);
    else
    {
        makevectors(self.v_angle);
        finish = start + v_forward * 96;
    }
    traceline(start, finish, FALSE, self);
    door = trace_ent;
    if (door.classname != "door") { self.lq_locked_door = world; self.lq_door_since = 0; return; }
    if (door.owner) door = door.owner;
    if (!door.targetname || door.health > 0) return;
    if (door.state == 0 || door.state == 2) return;
    if (self.lq_locked_door != door || vlen(self.origin - self.lq_door_origin) >= 24)
    {
        self.lq_locked_door = door;
        self.lq_door_since = time;
        self.lq_door_origin = self.origin;
    }
    // Ordinary trigger-open doors remain routable until physically proven stuck.
    if (!door.message && time - self.lq_door_since < 3) return;
    if (time - self.lq_door_since >= 3) door.lq_door_block_until = time + 60;
    // Find an actual player-operated activator, not another door in its group.
    activator = lq_find_activator(door, 0);
    if (!activator)
    {
        if (time - self.lq_door_since >= 3)
        {
            door.lq_cooldown = time + 60;
            self.lq_progress_time = self.lq_cell_time = time - 9;
            self.lq_retry_time = 0;
            lq_recover();
            self.lq_manoeuvre_start = time;
            self.lq_manoeuvre_count = self.lq_manoeuvre_count + 1;
        }
        return;
    }
    if (self.last_way && self.target1.classname == "waypoint")
    {
        self.last_way.lq_blocked_way = self.target1;
        self.last_way.lq_blocked_until = time + 120;
    }
    self.lq_activator = activator;
    self.target1 = self.target2 = self.target3 = self.target4 = world;
    ClearMyRoute();
    self.b_aiflags = 0;
    target_add(activator);
    if (activator.health > 0 && fisible(activator))
    {
        self.enemy = activator;
        bot_weapon_switch(1);
    }
    else bot_get_path(activator, TRUE);
    dprint("Live Quake: routing to linked door activator\n");
};

// Runs after BotAI, beneath its goal and route states. These are movement
// inputs consumed by FrikBot's ordinary player physics on the following tick.
void() lq_unstick =
{
    local vector delta;
    local float phase;
    if (self.ishuman || self.lq_parked || self.health <= 0 || (deathmatch && time - self.lq_fight_time < 3)) return;
    delta = self.origin - self.lq_motion_origin;
    delta_z = 0;
    if (!self.lq_motion_time || vlen(delta) >= 24)
    {
        self.lq_motion_origin = self.origin;
        self.lq_motion_time = time;
        if (!self.lq_manoeuvre_start)
        {
            self.lq_manoeuvre_count = 0;
            self.lq_stuck_since = 0;
        }
    }
    if (!self.lq_manoeuvre_start && time - self.lq_motion_time >= 2.5)
    {
        if (!self.lq_stuck_since) self.lq_stuck_since = self.lq_motion_time;
        if (time - self.lq_stuck_since >= 20)
        {
            self.lq_checkpoint_needed = 1;
            self.lq_stuck_since = time;
        }
        self.lq_manoeuvre_count = self.lq_manoeuvre_count + 1;
        self.lq_stuck_event = self.lq_stuck_event + 1;
        self.lq_manoeuvre_start = time;
        if (self.lq_manoeuvre_count > 3)
        {
            self.lq_progress_time = time - 9;
            self.lq_retry_time = 0;
            lq_recover();
            self.lq_manoeuvre_count = 1;
        }
    }
    if (!self.lq_manoeuvre_start) return;
    phase = time - self.lq_manoeuvre_start;
    self.b_aiflags = 0;
    self.enemy = world;
    self.button2 = TRUE;
    self.keys = 0;
    self.oldkeys = -1;
    if (phase < 0.55) self.keys = KEY_MOVEBACK;
    else if (phase < 1.55)
    {
        if (self.lq_manoeuvre_count & 1) self.keys = KEY_MOVELEFT;
        else self.keys = KEY_MOVERIGHT;
    }
    else self.keys = KEY_MOVEFORWARD;
    if (self.waterlevel >= 2) self.keys = self.keys | KEY_MOVEUP;
    if (phase >= 2)
    {
        self.lq_manoeuvre_start = 0;
        self.lq_motion_origin = self.origin;
        self.lq_motion_time = time;
    }
};

// Goal safety independent of FrikBot's item list and route-table completion.
void() lq_drive =
{
    local entity exit, best, human;
    local float distance, bestdistance;
    if (self.target1.classname == "trigger_once" || self.target1.classname == "trigger_multiple")
        if (!self.target1.target && !self.target1.killtarget)
        {
            self.target1.lq_cooldown = time + 120;
            self.target1 = self.target2 = self.target3 = self.target4 = world;
            self.b_aiflags = 0;
            self.lq_activator = world;
            self.lq_empty_since = time - 3;
        }
    if (self.target1) self.lq_empty_since = 0;
    else if (!self.lq_empty_since) self.lq_empty_since = time;
    if (!deathmatch && time >= 60 && time >= self.lq_exit_retry)
    {
        if (lq_e1m1_assist()) return;
        if (self.route_failed && self.lq_exit_goal)
        {
            self.lq_exit_failures = self.lq_exit_failures + 1;
            self.route_failed = 0;
        }
        if (self.lq_exit_failures >= 2) return;
        exit = find(world, classname, "trigger_changelevel");
        bestdistance = 100000;
        while (exit)
        {
            distance = vlen(realorigin(exit) - self.origin);
            if (distance < bestdistance) { best = exit; bestdistance = distance; }
            exit = find(exit, classname, "trigger_changelevel");
        }
        if (best && !target_onstack(best))
        {
            self.lq_exit_goal = best;
            self.lq_exit_retry = time + 2;
            self.target1 = self.target2 = self.target3 = self.target4 = world;
            self.enemy = world;
            self.b_aiflags = 0;
            ClearMyRoute();
            if (route_table == self) { route_table = world; busy_waypoints = 0; }
            target_add(best);
            bot_get_path(best, TRUE);
            return;
        }
    }
    if (!self.target1 && time - self.lq_empty_since >= 3)
    {
        self.lq_progress_time = self.lq_cell_time = time - 9;
        self.lq_retry_time = 0;
        lq_recover();
        self.lq_empty_since = time;
    }
};

void() lq_water_escape =
{
    local entity way, best;
    local float distance, bestdistance;
    if (self.waterlevel < 2) return;
    self.lq_water_avoid_until = time + 60;
    bestdistance = 100000;
    way = way_head;
    while (way)
    {
        if (lq_dry_way(way) && way.origin_z > self.origin_z + 16 && way.lq_cooldown <= time)
        {
            distance = vlen(way.origin - self.origin);
            if (distance < bestdistance) { best = way; bestdistance = distance; }
        }
        way = way._next;
    }
    self.enemy = world;
    self.b_aiflags = 0;
    self.keys = KEY_MOVEUP | KEY_MOVEFORWARD;
    self.oldkeys = -1;
    self.button2 = TRUE;
    if (best && bestdistance < 1200)
    {
        self.target1 = self.target2 = self.target3 = self.target4 = world;
        ClearMyRoute();
        target_add(best);
        bot_get_path(best, TRUE);
    }
};

// Optional ambient assist for a switchable START_OFF light: activate only the
// map's own matching player trigger, once. The resulting world survives takeover.
void() lq_light_assist =
{
    local entity light, trigger, bot, saved_other;
    if (deathmatch) return;
    bot = self;
    saved_other = other;
    light = find(world, classname, "light");
    while (light)
    {
        if ((light.spawnflags & 1) && light.targetname && !light.lq_ambient_lit)
            if (vlen(light.origin - bot.origin) < 400)
            {
                trigger = find(world, target, light.targetname);
                while (trigger)
                {
                    if ((trigger.classname == "trigger_once" || trigger.classname == "trigger_multiple") && trigger.touch)
                    {
                        light.lq_ambient_lit = TRUE;
                        self = trigger;
                        other = bot;
                        trigger.touch();
                        self = bot;
                        other = saved_other;
                        break;
                    }
                    trigger = find(trigger, target, light.targetname);
                }
            }
        light = find(light, classname, "light");
    }
};
