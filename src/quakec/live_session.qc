// SPDX-License-Identifier: GPL-2.0-or-later
// Atomic transfers happen only on takeover/handback, never for navigation.
void(entity source, entity destination) LQ_CopyPlayer =
{
    destination.health = source.health;
    destination.armorvalue = source.armorvalue;
    destination.armortype = source.armortype;
    destination.items = source.items;
    destination.weapon = source.weapon;
    destination.weaponmodel = source.weaponmodel;
    destination.weaponframe = source.weaponframe;
    destination.currentammo = source.currentammo;
    destination.ammo_shells = source.ammo_shells;
    destination.ammo_nails = source.ammo_nails;
    destination.ammo_rockets = source.ammo_rockets;
    destination.ammo_cells = source.ammo_cells;
    destination.invincible_finished = source.invincible_finished;
    destination.invisible_finished = source.invisible_finished;
    destination.super_damage_finished = source.super_damage_finished;
    destination.radsuit_finished = source.radsuit_finished;
    destination.attack_finished = source.attack_finished;
    destination.pausetime = source.pausetime;
    destination.deadflag = source.deadflag;
    destination.velocity = source.velocity;
    destination.view_ofs = source.view_ofs;
    destination.angles = source.angles;
    destination.v_angle = source.v_angle;
    destination.flags = source.flags - (source.flags & FL_NOTARGET);
    setorigin(destination, source.origin);
};

// Restore a body without spawn selection, teleport fog or a telefrag.
.entity lq_parked_bot;
void() LQ_InitBody =
{
    self.classname = "player";
    self.takedamage = DAMAGE_AIM;
    self.solid = SOLID_SLIDEBOX;
    self.movetype = MOVETYPE_WALK;
    self.show_hostile = 0;
    self.max_health = 100;
    self.flags = (self.flags | FL_CLIENT) - (self.flags & FL_NOTARGET);
    self.air_finished = time + 12;
    self.dmg = 2;
    self.effects = self.invincible_time = self.pain_finished = self.super_time = 0;
    self.th_pain = player_pain;
    self.th_die = PlayerDie;
    setmodel(self, "progs/player.mdl");
    modelindex_player = self.modelindex;
    setsize(self, VEC_HULL_MIN, VEC_HULL_MAX);
    player_stand1();
};
void() LQ_EnterCamera =
{
    local entity bot, human;
    human = self;
    self.ishuman = TRUE;
    self.b_clientno = 0;
    active_clients = active_clients | 1;
    if (self.classname == "botcam") return;
    if (self.classname != "player") return;
    bot = self.lq_parked_bot;
    if (bot && bot.lq_parked)
    {
        self = bot;
        LQ_InitBody();
        self = human;
        LQ_CopyPlayer(human, bot);
        bot.frags = human.frags;
        bot.b_frags = bot.frags;
        human.frags = human.b_frags = 0;
        UpdateClient(bot);
        bot.lq_parked = FALSE;
        if (deathmatch && bot.health <= 0)
        {
            // respawn copies this body into a non-client corpse entity. WALK
            // is valid for the client slot, but not for that corpse's physics.
            bot.movetype = MOVETYPE_TOSS;
            self = bot; respawn(); self = human;
        }
    }
    else
    {
        if (!deathmatch && !bot_count) BotConnect(0, 0, 1);
        bot = player_head;
        while (bot && (bot.ishuman || bot.lq_parked)) bot = bot._next;
        if (!bot) return;
        if (!deathmatch) LQ_CopyPlayer(self, bot);
    }
    self.lq_parked_bot = world;
    if (bot.phys_obj)
    {
        setorigin(bot.phys_obj, bot.origin);
        bot.phys_obj.dest2 = bot.origin;
        bot.phys_obj.dest1 = bot.phys_obj.velocity = bot.velocity;
        bot.phys_obj.movetype = MOVETYPE_STEP;
        bot.phys_obj.solid = SOLID_TRIGGER;
        bot.phys_obj.think = PostPhysics;
        bot.phys_obj.nextthink = time + 0.05;
    }
    self.enemy = bot;
    self.classname = "botcam";
    self.solid = SOLID_NOT;
    self.movetype = MOVETYPE_NONE;
    self.takedamage = DAMAGE_NO;
    self.flags = self.flags | FL_NOTARGET;
    self.modelindex = 0;
    self.velocity = '0 0 0';
    self.button0 = self.button1 = self.button2 = self.impulse = 0;
    self.v_angle = bot.v_angle;
    botcam();
};

float lq_dm_connect_after;
void() LQ_ScoreMarker;
void() LQ_DmTopUp =
{
    local entity human, candidate;
    local float slot, number, present, wanted, skill;
    LQ_ScoreMarker();
    if (!deathmatch || intermission_running || time < 1 || time < lq_dm_connect_after) return;
    wanted = lq_dm_bots;
    if (wanted < 1) wanted = 4;
    if (bot_count >= wanted) return;
    human = self;
    slot = 1;
    while (slot <= wanted)
    {
        number = slot;
        if (slot == 5) number = 6;
        if (slot == 6) number = 7;
        if (slot == 7) number = 15;
        present = FALSE;
        candidate = player_head;
        while (candidate)
        {
            if (!candidate.ishuman && candidate.b_num == number) present = TRUE;
            candidate = candidate._next;
        }
        if (!present)
        {
            skill = cvar("skill");
            if (skill > 2) skill = 2;
            if (skill < 0) skill = 0;
            BotConnect(0, number, skill);
            lq_dm_connect_after = time + 1;
            self = human;
            return;
        }
        slot = slot + 1;
    }
    self = human;
};

vector lq_intermission_origin, lq_intermission_angles;
void() LQ_MaintainCamera =
{
    if (intermission_running)
    {
        if (!lq_intermission_until)
        {
            local entity spot;
            lq_intermission_until = time + 5;
            spot = FindIntermission();
            lq_intermission_origin = spot.origin;
            lq_intermission_angles = spot.mangle;
        }
        self.view_ofs = '0 0 0';
        self.angles = self.v_angle = lq_intermission_angles;
        self.fixangle = TRUE;
        setorigin(self, lq_intermission_origin);
        msg_entity = self;
        WriteByte(MSG_ONE, 5);
        WriteEntity(MSG_ONE, self);
        WriteByte(MSG_ONE, 10);
        WriteAngle(MSG_ONE, self.v_angle_x);
        WriteAngle(MSG_ONE, self.v_angle_y);
        WriteAngle(MSG_ONE, self.v_angle_z);
        if (time >= lq_intermission_until) GotoNextMap();
        return;
    }
    self.ishuman = TRUE;
    self.b_clientno = 0;
    active_clients = active_clients | 1;
    if (self.classname != "botcam") LQ_EnterCamera();
    if (self.classname != "botcam") return;
    if (self.enemy.health <= 0 && self.enemy.deadflag == DEAD_DEAD)
    {
        local entity human;
        human = self;
        self = self.enemy;
        respawn();
        if (self.phys_obj)
        {
            setorigin(self.phys_obj, self.origin);
            self.phys_obj.dest2 = self.origin;
            self.phys_obj.dest1 = self.phys_obj.velocity = self.velocity;
        }
        self = human;
    }
    self.solid = SOLID_NOT;
    self.movetype = MOVETYPE_NONE;
    self.takedamage = DAMAGE_NO;
    self.flags = self.flags | FL_NOTARGET;
    self.modelindex = 0;
    self.velocity = '0 0 0';
    self.button0 = self.button1 = self.button2 = self.impulse = 0;
    botcam();
};

void() LQ_WaterRecover =
{
    local entity human;
    human = self;
    if (self.classname != "botcam") return;
    self = self.enemy;
    lq_water_escape();
    self = human;
};

void() LQ_SkipLevel =
{
    local entity human, exit, saved_other;
    human = self;
    if (deathmatch || self.classname != "botcam" || intermission_running) return;
    exit = find(world, classname, "trigger_changelevel");
    if (!exit || !exit.touch) return;
    saved_other = other;
    other = self.enemy;
    self = exit;
    exit.touch();
    self = human;
    other = saved_other;
};

void() LQ_Takeover =
{
    local entity bot;
    if (self.classname != "botcam") return;
    bot = self.enemy;
    if (!bot || bot.health <= 0 || bot.lq_parked) return;
    LQ_InitBody();
    LQ_CopyPlayer(bot, self);
    self.frags = bot.frags;
    bot.frags = bot.b_frags = 0;
    UpdateClient(bot);
    self.lq_parked_bot = bot;
    self.enemy = world;
    self.fixangle = TRUE;
    self.button0 = self.button1 = self.button2 = self.impulse = 0;
    bot.lq_parked = TRUE;
    bot.solid = SOLID_NOT;
    bot.movetype = MOVETYPE_NONE;
    bot.takedamage = DAMAGE_NO;
    bot.flags = bot.flags | FL_NOTARGET;
    bot.modelindex = 0;
    bot.velocity = '0 0 0';
    bot.nextthink = 0;
    if (bot.phys_obj)
    { bot.phys_obj.movetype = MOVETYPE_NONE; bot.phys_obj.solid = SOLID_NOT;
      bot.phys_obj.velocity = '0 0 0'; bot.phys_obj.nextthink = 0; }
    msg_entity = self;
    WriteByte(MSG_ONE, 5);
    WriteEntity(MSG_ONE, self);
    WriteByte(MSG_ONE, 10);
    WriteAngle(MSG_ONE, self.v_angle_x);
    WriteAngle(MSG_ONE, self.v_angle_y);
    WriteAngle(MSG_ONE, self.v_angle_z);
};

void() LQ_Recover =
{
    local entity human;
    human = self;
    if (self.classname != "botcam") return;
    self = self.enemy;
    self.lq_retry_time = 0;
    self.lq_progress_time = time - 9;
    self.lq_cell_time = time - 9;
    lq_recover();
    self = human;
};

void() LQ_ForceRecovery =
{
    local entity human;
    human = self;
    if (self.classname != "botcam") return;
    self = self.enemy;
    self.lq_retry_time = 0;
    self.lq_progress_time = self.lq_cell_time = time - 9;
    lq_recover();
    self.lq_manoeuvre_count = self.lq_manoeuvre_count + 1;
    self.lq_manoeuvre_start = time;
    self.lq_stuck_event = self.lq_stuck_event + 1;
    lq_unstick();
    self = human;
};

void() LQ_AmbientLight =
{
    local entity human;
    human = self;
    if (self.classname != "botcam") return;
    self = self.enemy;
    lq_light_assist();
    self = human;
};

.entity lq_nudge_bot;
void() LQ_NudgeBot =
{
    local entity human;
    human = self;
    if (!self.lq_nudge_bot || self.lq_nudge_bot.lq_parked) return;
    self = self.lq_nudge_bot;
    self.lq_retry_time = 0;
    self.lq_progress_time = self.lq_cell_time = time - 9;
    lq_recover();
    self.lq_manoeuvre_start = time;
    self.lq_manoeuvre_count = self.lq_manoeuvre_count + 1;
    self = human;
    self.lq_nudge_bot = world;
};

// While a human plays, the scoreboard row with the body's frags reads "You" and the
// parked bot's row is hidden (the stock scoreboard skips empty names). Names go out
// once per change; a map change clears the field, so a held takeover is marked again.
.float lq_score_marked;
void() LQ_ScoreMarker =
{
    local entity human, parked;
    human = nextent(world);
    if (human.classname == "player" && human.ishuman)
    {
        if (human.lq_score_marked) return;
        WriteByte(MSG_ALL, SVC_UPDATENAME);
        WriteByte(MSG_ALL, 0);
        WriteString(MSG_ALL, "You");
        parked = human.lq_parked_bot;
        if (parked && parked.lq_parked)
        {
            WriteByte(MSG_ALL, SVC_UPDATENAME);
            WriteByte(MSG_ALL, parked.b_clientno);
            WriteString(MSG_ALL, "");
        }
        human.lq_score_marked = TRUE;
    }
    else if (human.lq_score_marked)
    {
        WriteByte(MSG_ALL, SVC_UPDATENAME);
        WriteByte(MSG_ALL, 0);
        WriteString(MSG_ALL, human.netname);
        human.lq_score_marked = FALSE;
    }
};
