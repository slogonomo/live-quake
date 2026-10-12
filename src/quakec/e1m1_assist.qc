// SPDX-License-Identifier: GPL-2.0-or-later
// LOCAL per-map assist: original e1m1's shootable rising bridge shortcut.
// Own supplement; never rewrites the third-party .way/.wa1 files.
// EXPERIMENTAL, OFF by default (lq_e1m1_assist 0). Not in the morning candidate.
// v23/v24 shoot t4, ride the platform and latch crossed, but stage 3 fails
// to follow the upper exit chain and the bot falls back to the lower floor.
// Handback briefly reports stage 0 on the newly spawned bot; the map-global
// latch is restored on its next assist call, but route continuity is unproven.
// Dry waypoint endpoints do not guarantee dry movement between them.
// No recorded run touches the real slipgate: do not enable by default.
.float lq_e1m1_stage;
.float lq_e1m1_route_retry;
.float lq_e1m1_buttons;
entity lq_e1m1_shoot, lq_e1m1_lower, lq_e1m1_upper;
entity lq_e1m1_exit;
float lq_e1m1_map_stage, lq_e1m1_map_buttons;

float() lq_e1m1_aim =
{
    local vector aim, delta;
    if (mapname != "e1m1" || self.lq_e1m1_stage != 1 || self.enemy.classname != "trigger_multiple" || self.enemy.target != "t4") return FALSE;
    aim = vectoangles((self.enemy.absmin + self.enemy.absmax) * 0.5 - (self.origin + self.view_ofs));
    aim_x = 0 - aim_x;
    delta = aim - self.v_angle;
    while (delta_y > 180) delta_y = delta_y - 360;
    while (delta_y < -180) delta_y = delta_y + 360;
    while (delta_x > 180) delta_x = delta_x - 360;
    while (delta_x < -180) delta_x = delta_x + 360;
    if (delta_y > 3) delta_y = 3;
    if (delta_y < -3) delta_y = -3;
    if (delta_x > 3) delta_x = 3;
    if (delta_x < -3) delta_x = -3;
    self.v_angle = self.v_angle + delta;
    self.angles = self.v_angle;
    self.angles_x = 0;
    self.keys = 0;
    self.b_angle = aim;
    self.impulse = 2; // stock shotgun selection, only for this local objective
    self.button0 = TRUE;
    return TRUE;
};

entity(float number) lq_e1m1_way =
{
    local entity way;
    way = way_head;
    while (way && way.count != number) way = way._next;
    return way;
};

float() lq_e1m1_assist =
{
    local entity bridge, trigger, way, goal;
    // Experimental objective is explicitly opt-in, never a morning default.
    if (!cvar("lq_e1m1_assist") || mapname != "e1m1" || time < 60) return FALSE;
    if (!lq_e1m1_map_stage && self.waterlevel >= 2) return FALSE;
    self.lq_e1m1_stage = lq_e1m1_map_stage;
    self.lq_e1m1_buttons = lq_e1m1_map_buttons;
    bridge = find(world, targetname, "t4");
    if (!bridge) return FALSE;
    trigger = find(world, target, "t4");
    while (trigger && trigger.classname != "trigger_multiple") trigger = find(trigger, target, "t4");
    if (!trigger) return FALSE;
    if (!lq_e1m1_shoot)
    {
        // Stand on the moving brush before shooting: the raised step is 66u,
        // beyond an ordinary jump from the floor beside it.
        lq_e1m1_shoot = make_waypoint('544 2112 -168');
        lq_e1m1_shoot.b_aiflags = AI_PRECISION;
        lq_e1m1_lower = lq_e1m1_way(32);
        lq_e1m1_upper = lq_e1m1_way(54);
        // Stock 53->54 is a one-way drop/retreat edge. The risen bridge
        // provides the hull-traced northbound connection into the upper hall.
        if (lq_e1m1_upper) lq_e1m1_upper.target3 = lq_e1m1_way(53);
        if (lq_e1m1_lower) lq_e1m1_lower.target2 = lq_e1m1_shoot;
        lq_e1m1_shoot.target1 = lq_e1m1_lower;
        // Split the long final ramp and connect the final slipgate approach.
        way = make_waypoint('1312 1472 -360');
        way.target1 = lq_e1m1_way(89);
        way.target2 = lq_e1m1_way(90);
        if (way.target1) way.target1.target2 = way;
        if (way.target2) way.target2.target2 = way;
        way = make_waypoint('1312 704 -234');
        way.target1 = lq_e1m1_way(1);
        if (way.target1) way.target1.target4 = way;
        goal = make_waypoint('1312 544 -234');
        way.target2 = goal;
        goal.target1 = way;
        lq_e1m1_exit = goal;
    }
    if (self.origin_x >= 512 && self.origin_x <= 640 && self.origin_y >= 2180 &&
        self.origin_y <= 2300 && self.origin_z >= -110) self.lq_e1m1_stage = 3;
    if (self.lq_e1m1_stage == 3) lq_e1m1_map_stage = 3;
    if (self.lq_e1m1_stage == 3)
    {
        goal = lq_e1m1_exit;
        // Commit to the northbound upper corridor, never a brush-origin goal.
        if (self.origin_y < 2300 && self.origin_z > -130)
            goal = lq_e1m1_way(53);
    }
    else if (bridge.origin_z > -1)
    {
        // Only add the rising-bridge link once the actual brush has risen.
        lq_e1m1_shoot.target2 = lq_e1m1_upper;
        self.lq_e1m1_stage = 2;
        goal = lq_e1m1_upper;
    }
    else
    {
        self.lq_e1m1_stage = 1;
        goal = lq_e1m1_shoot;
    }
    lq_e1m1_map_stage = self.lq_e1m1_stage;
    if (self.lq_e1m1_stage == 1 && self.origin_x >= 528 && self.origin_x <= 560 &&
        self.origin_y >= 1936 && self.origin_y <= 2160 && fisible(trigger))
    {
        self.enemy = trigger;
        self.lq_activator = trigger;
        bot_weapon_switch(1);
        return TRUE;
    }
    if (!target_onstack(goal) || time >= self.lq_e1m1_route_retry)
    {
        self.lq_e1m1_route_retry = time + 3;
        self.target1 = self.target2 = self.target3 = self.target4 = world;
        self.enemy = world;
        self.lq_activator = world;
        self.b_aiflags = 0;
        ClearMyRoute();
        if (route_table == self) { route_table = world; busy_waypoints = 0; }
        target_add(goal);
        bot_get_path(goal, TRUE);
        self.lq_assist_dry_routing = TRUE;
        if (self.lq_e1m1_stage == 3 && goal == lq_e1m1_exit) self.lq_exit_routing = TRUE;
    }
    return TRUE;
};
