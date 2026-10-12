#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build Live Quake's bot pack. Fetches nothing and includes no game data.

Compiles id's pinned GPL QuakeC v1.01 (Quake-Tools) with FrikBot X 0.10.2 and our session QuakeC, using the pinned
fteqcc. FrikBot X and the Quake Bot Archive's episode waypoints are included in third_party/ and used by default.
This script carries splices of id's GPL QuakeC, so it is GPL-2.0-or-later like patches/ironwail-desktop.patch.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import struct

ROOT = Path(__file__).resolve().parents[1]
FRIKBOT = ROOT / 'third_party/frikbot-x'
WAYPOINTS = ROOT / 'third_party/quake-bot-archive/waypoints'
WAYPOINT_NAME = re.compile(r'(?:e[1-4]m[1-8]|start|end)\.wa(?:y|[1-9])')
WAYPOINT_CREDIT = (b'Waypoints for start, end and e1m1-e4m8 by [bta].monster (e1m7 by /\\/\\O/\\/sT3rTU), from the\n'
                   b'FrikBot X package in the Quake Bot Archive, https://github.com/Jason2Brownlee/QuakeBotArchive.\n'
                   b'Thanks to Jason Brownlee for preserving Quake bot history that would otherwise disappear.\n')
BASE_PIN = 'c0d1b91c74eb654365ac7755bc837e497caaca73'
BASE_PATH = 'qcc/v101qc'
COMPILER_PIN = 'f937b9d88f71fc4429db5fe56c6a98d922711b2e'

def replace_once(text, old, new, name="FrikBot splice"):
    if text.count(old) != 1:
        raise ValueError(f'{name}: expected one match for {old[:40]!r}')
    return text.replace(old, new)

# Single-player rules in our local coop session. Deathmatch retains id's rules;
# FrikBot still needs coop for monster targeting and its waypoint graph.
KEEP_ON_RESPAWN = 'IT_KEY1 | IT_KEY2 | IT_SUPER_SHOTGUN | IT_NAILGUN | IT_SUPER_NAILGUN | IT_GRENADE_LAUNCHER | IT_ROCKET_LAUNCHER | IT_LIGHTNING'
SINGLE_PLAYER_EDITS = {
    'items.qc': [
        ('\tif (deathmatch == 2 || coop)\n\t\tleave = 1;\n',
         '\tif (deathmatch == 2)\n\t\tleave = 1;\n'),
        ('\tif (!coop)\n\t{\t\n\t\tself.solid = SOLID_NOT;\n\t\tself.model = string_null;\n\t}\n',
         '\tself.solid = SOLID_NOT;\n\tself.model = string_null;\n'),
    ],
    'client.qc': [
        ('void() respawn =\n{\n\tif (coop)\n',
         'void() respawn =\n{\n\t// Live Quake: the bot respawns coop-style; a human death restarts the level, as in single player.\n\tif (coop && !self.ishuman)\n'),
        ('\tbprint (other.netname);\n\tbprint (" exited the level\\n");\n',
         '\tif (deathmatch)\n\t{\n\t\tbprint (other.netname);\n\t\tbprint (" exited the level\\n");\n\t}\n'),
        ('\t\tsetspawnparms (self);\n',
         '\t\tsetspawnparms (self);\n\t\t// Keys and weapons vanish when taken, so a respawn keeps the ones found on this level.\n\t\tparm1 = parm1 | (self.items & (' + KEEP_ON_RESPAWN + '));\n'),
        ('// choose a info_player_deathmatch point\n\tif (coop)\n',
         '// choose a info_player_deathmatch point\n\tif (coop && !self.ishuman)\n'),
        ('\tif (deathmatch || coop)\n\t{\n\t\tmakevectors(self.angles);',
         '\tif (deathmatch)\n\t{\n\t\tmakevectors(self.angles);'),
    ],
    'player.qc': [
        ('\tif (deathmatch || coop)\n\t\tDropBackpack();\n',
         '\tif (deathmatch || (coop && !self.ishuman))\n\t\tDropBackpack();\n'),
    ],
    'weapons.qc': [
        ('void() CheatCommand =\n{\n\tif (deathmatch || coop)\n',
         'void() CheatCommand =\n{\n\tif (deathmatch)\n'),
        ('void() QuadCheat =\n{\n\tif (deathmatch || coop)\n',
         'void() QuadCheat =\n{\n\tif (deathmatch)\n'),
    ],
    'frikbot/bot.qc': [
        ('\tself.armorvalue = self.enemy.armorvalue;\n',
         '\tself.armorvalue = self.enemy.armorvalue;\n\tself.armortype = self.enemy.armortype;\n'),
    ],
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--frikbot', type=Path, default=FRIKBOT, help='FrikBot X 0.10.2 folder (default: third_party)')
    parser.add_argument('--waypoints', type=Path, default=WAYPOINTS, help='episode waypoint files (default: third_party)')
    parser.add_argument('--compiler-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for repo, pin in ((args.base, BASE_PIN), (args.compiler_source, COMPILER_PIN)):
        actual = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != pin: raise ValueError(f'Unexpected source pin: {repo}')
    compiler = args.compiler_source.resolve() / 'engine/qclib/fteqcc.bin'
    if not compiler.exists():
        subprocess.run(['make', '-C', str(compiler.parent), '-j8', 'qcc'], check=True, timeout=120)
    out = args.output.resolve()
    qc = out / 'qc-local'
    qc.mkdir(parents=True, exist_ok=True)
    for source in (args.base / BASE_PATH).glob('*'):
        shutil.copy2(source, qc / source.name)
    for name in ('frikbot', 'waypoints'):
        shutil.copytree(args.frikbot / 'src' / name, qc / name, dirs_exist_ok=True)
    program = (qc / 'progs.src').read_text().replace('../progs.dat', 'progs.dat')
    bot_files = ['bot', 'bot_way', 'bot_fight', 'bot_ai', 'bot_misc', 'bot_phys', 'bot_move', 'bot_ed']
    includes = ''.join(f'waypoints/map_dm{n}.qc\n' for n in range(1, 7))
    includes += ''.join(f'frikbot/{name}.qc\n' for name in bot_files)
    includes = includes.replace('frikbot/bot_ai.qc', 'e1m1_assist.qc\nlive_bot.qc\nfrikbot/bot_ai.qc')
    shutil.copy2(ROOT / 'src/quakec/e1m1_assist.qc', qc / 'e1m1_assist.qc')
    (qc / 'progs.src').write_text(program.replace('defs.qc\n', 'defs.qc\n' + includes) + '\nlive_session.qc\n')
    shutil.copy2(ROOT / 'src/quakec/live_session.qc', qc / 'live_session.qc')
    names = 'sound stuffcmd sprint aim centerprint setspawnparms WriteByte WriteChar WriteShort WriteLong WriteCoord WriteAngle WriteString WriteEntity'.split()
    defs = (qc / 'defs.qc').read_text()
    defs = '\n'.join('// ' + line if re.search(r'\b(' + '|'.join(names) + r')\s*=\s*#', line)
                     and not line.lstrip().startswith('//') else line for line in defs.splitlines()) + '\n'
    (qc / 'defs.qc').write_text(defs)
    hooks = {'world.qc': {'worldspawn': 'BotInit();', 'StartFrame': 'BotFrame(); LQ_DmTopUp();'},
             'client.qc': {'PlayerPreThink': 'if (BotPreFrame()) return;',
                           'PlayerPostThink': 'if (BotPostFrame()) return;',
                           'ClientConnect': 'ClientInRankings();', 'ClientDisconnect': 'ClientDisconnected();'}}
    for filename, functions in hooks.items():
        text = (qc / filename).read_text()
        for name, code in functions.items():
            text, count = re.subn(r'(void\(\)\s*' + name + r'\s*=\s*\{)', r'\1\n    ' + code, text)
            if count != 1: raise ValueError(f'Missing hook: {name}')
        (qc / filename).write_text(text)
    additions = (ROOT / 'src/quakec/live_bot.qc').read_text()
    boundary = additions.index('void() lq_recover =')
    bot = qc / 'frikbot/bot.qc'
    bot_text = bot.read_text().replace('self.view_ofs = self.enemy.view_ofs;', 'self.v_angle = self.enemy.v_angle;\n    self.angles = self.enemy.angles;\n    self.view_ofs = self.enemy.view_ofs;')
    # The session bridge restores exactly one bot; FrikBot's independent coop
    # scratch-cvar restoration must not add another set after changelevel.
    bot_text = bot_text.replace('saved_bots = cvar("scratch1");', 'saved_bots = 0;')
    declarations = additions.index('// Called only by exit-route expansion')
    bot.write_text(additions[:declarations] + '\n' + bot_text + '\n' + additions[declarations:boundary])
    (qc / 'live_bot.qc').write_text(additions[boundary:])
    ways = qc / 'frikbot/bot_way.qc'
    ways_text = ways.read_text()
    for index in range(1, 5):
        call = f'FollowLink(self.target{index}, AI_TELELINK_{index});'
        ways_text = ways_text.replace(call, f'if ((!route_table.lq_exit_routing || lq_exit_link_open(self, self.target{index})) && (!route_table.lq_assist_dry_routing || lq_dry_way(self.target{index}))) ' + call)
    ways_text = ways_text.replace('if (direct)\n\t{', 'if (direct)\n\t{\n        self.lq_assist_dry_routing = FALSE;\n        self.lq_exit_routing = this.classname == "trigger_changelevel";')
    ways.write_text(ways_text)
    ai = qc / 'frikbot/bot_ai.qc'
    text = ai.read_text()
    text = replace_once(text, 'thisp = 0;', 'thisp = 0;\n    if (time < self.lq_water_avoid_until && thing.classname == "waypoint" && pointcontents(thing.origin) < -2) return 0;')
    # Brush entities have origin zero; losing an exit/button because it appears
    # above the player's head defeats the route before it can be followed.
    text = text.replace('local vector dist;\n\tdist = realorigin(targ)', 'local vector dist, target_origin;\n    target_origin = realorigin(targ);\n\tdist = realorigin(targ)')
    text = text.replace('targ.origin_z', 'target_origin_z').replace('targ.origin - self.origin', 'realorigin(targ) - self.origin').replace('self.origin - targ.origin', 'self.origin - realorigin(targ)')
    text = replace_once(text, 'thatp = priority_for_thing(foe);', 'thatp = priority_for_thing(foe);\n        if (!deathmatch && time >= 60 && target_onstack(find(world, classname, "trigger_changelevel")) && foe.classname != "trigger_changelevel" && vlen(realorigin(foe) - self.origin) > 256) thatp = 0;')
    for entity in ('newt', 'tele'):
        text = text.replace(f'//\tbot_get_path({entity}, TRUE);', f'bot_get_path({entity}, TRUE);')
    text = replace_once(text, 'thisp = 0;', 'thisp = 0;\n    if (thing.classname != "player" && thing.lq_cooldown > time) return 0;\n    if ((thing.classname == \"trigger_once\" || thing.classname == \"trigger_multiple\") && !thing.target && !thing.killtarget) return 0;')
    text = replace_once(text, 'else if (thing.classname == "waypoint")\n\t{', '''else if (thing.classname == "trigger_changelevel")
    {
        if (!deathmatch) { if (time >= 60) thisp = 1000; else thisp = 15; }
    }
    else if (thing.classname == "waypoint")
    {
        if (deathmatch) return 0;
        if (!lq_dry_way(thing)) return 0;
        if (self.target1 && time - self.lq_cell_time < 9) return 0;
        if (thing.items == -1) return 0;
        if (vlen(thing.origin - self.origin) < 64) return 0;
        thing.current_way = thing;
        if (!(thing.lq_seen & ClientBitFlag(self.b_clientno))) thisp = 8;
        else thisp = bound(0, (time - thing.lq_visit_time) * 0.2, 30);
''')
    # Vanilla QC has no bound builtin; spell the clamp out explicitly.
    text = text.replace('else thisp = bound(0, (time - thing.lq_visit_time) * 0.2, 30);',
                        'else { thisp = (time - thing.lq_visit_time) * 0.2; if (thisp > 30) thisp = 30; }')
    text = replace_once(text, '// target1 is like goalentity in normal Quake monster AI.',
                        '''lq_recover();
    if (self.lq_escape_way && time < self.lq_escape_until)
    {
        self.target1 = self.lq_escape_way;
        self.b_aiflags = AI_BLIND;
        frik_movetogoal();
        bot_angle_set();
        if (self.waterlevel >= 2)
        {
            self.keys = self.keys | KEY_MOVEUP;
            self.button2 = TRUE;
            if (self.waterlevel == 2) self.keys = self.keys | KEY_MOVEFORWARD;
        }
        if (vlen(self.target1.origin - self.origin) < 48)
        {
            self.last_way = self.target1;
            self.lq_escape_way = world;
            self.target1 = world;
            self.b_aiflags = 0;
        }
        return;
    }
    self.lq_escape_way = world;
    lq_drive();
    if (!self.lq_e1m1_stage) lq_linked_door();

    // target1 is like goalentity in normal Quake monster AI.''')
    # Stock fallback can re-add the same cooldown activator immediately.
    text = text.replace('else if (trace_ent.targetname)',
                        'else if (trace_ent.targetname && tele.lq_cooldown <= time)')
    text = text.replace('target_add(tele);\n\t\t\t\t\tbot_get_path(tele, TRUE);',
                        'if (tele.lq_cooldown <= time) { target_add(tele); bot_get_path(tele, TRUE); }')
    text = text.replace('target_add(newt);\n\t\t\t\t\tbot_get_path(newt, TRUE);',
                        'if (newt.lq_cooldown <= time) { target_add(newt); bot_get_path(newt, TRUE); }')
    # Preserve normal mouse/key turning rather than stock instant goal turns.
    text = text.replace('if (self.b_skill == 3)\n\t{\n\t\tself.keys = self.keys & 63;',
                        'if (FALSE)\n\t{\n\t\tself.keys = self.keys & 63;')
    text = text.replace('else if ((self.enemy == world || self.enemy.movetype == MOVETYPE_PUSH) && self.target1.classname != "player")',
                        'else if (FALSE)')
    text = text.replace('bot_angle_set();', 'if (!lq_e1m1_aim()) bot_angle_set();')
    ai.write_text(text)
    ways = qc / 'frikbot/bot_way.qc'
    text = ways.read_text().replace('local float dist;\n\t\n\tif (self.b_aiflags & b_bit)',
        'local float dist;\n    if (e2.lq_cooldown > time) return;\n    if (self.lq_blocked_way == e2 && self.lq_blocked_until > time) return;\n\t\n\tif (self.b_aiflags & b_bit)')
    ways.write_text(text)
    misc = qc / 'frikbot/bot_misc.qc'
    misc.write_text(misc.read_text().replace('traceline (self.origin, targ.origin, TRUE, self);', 'traceline (realorigin(self), realorigin(targ), TRUE, self);'))
    combat = qc / 'combat.qc'
    combat.write_text(combat.read_text().replace('targ.health = targ.health - take;',
        'if (deathmatch && targ.classname == "player" && attacker.classname == "player") { targ.lq_fight_time = time; attacker.lq_fight_time = time; }\n    if (((targ.flags & FL_MONSTER) || (deathmatch && targ.classname == "player")) && targ.health > 0 && attacker.classname == "player" && !attacker.ishuman) attacker.lq_damage_dealt = attacker.lq_damage_dealt + take;\n    targ.health = targ.health - take;'))
    physics = qc / 'frikbot/bot_phys.qc'
    physics_text = physics.read_text()
    if physics_text.count('BotAI();') != 4: raise ValueError('Expected four physics AI hooks')
    physics_text = physics_text.replace('BotAI();', 'BotAI();\n    lq_unstick();')
    # Failed step probes must restore their start even when droptofloor moved
    # farther than four units. Never use the stock force-through-wall fallback.
    physics_text = physics_text.replace('if (vlen(v - self.origin) <= 4)\n\t\tsetorigin(self, currentorigin);',
                                        'setorigin(self, currentorigin);')
    physics_text = physics_text.replace('BruteForceStep(self.phys_obj.dest1);',
                                        'self.velocity_x = self.velocity_y = 0;')
    physics.write_text(physics_text)
    client = qc / 'client.qc'
    client_text = client.read_text().replace('void() PlayerJump =\n{',
        'void() PlayerJump =\n{\n    if (!self.ishuman) { if (time < self.lq_jump_until) return; self.lq_jump_until = time + 0.6; }')
    client_text = client_text.replace('intermission_exittime = time + 2;', 'intermission_exittime = time + 5;')
    client_text, count = re.subn(r'bprint\s*\(self\.netname\);\s*bprint\s*\(" entered the game\\n"\);',
        lambda match: 'if ((!deathmatch && self.ishuman == 1) || (deathmatch && self.ishuman != 1)) { bprint(self.netname); bprint(" entered the game\\n"); }', client_text)
    if count != 1: raise ValueError('Missing ClientConnect notification hook')
    client_text = client_text.replace('// let everyone else know', 'if ((!deathmatch && !self.ishuman) || (deathmatch && self.ishuman == 1)) return;\n    // let everyone else know')
    client.write_text(client_text)
    # Parked clients retain their slot and bot_count, but execute no AI or rules.
    for filename, anchor in (('frikbot/bot_ai.qc', 'void() BotAI =\n{'),
            ('client.qc', 'void() PlayerPreThink =\n{'),
            ('client.qc', 'void() CheckRules =\n{')):
        path = qc / filename
        path.write_text(replace_once(path.read_text(), anchor, anchor + '\n    if (self.lq_parked) return;', filename))
    path = qc / 'frikbot/bot.qc'
    text = replace_once(path.read_text(), 'if (self.ishuman == FALSE)',
        'if (self.ishuman == FALSE && !self.lq_parked)', 'parked BotFrame')
    text = replace_once(text, 'void() bot_map_load =\n{', 'void() bot_map_load =\n{\n    if (deathmatch && lq_rerelease) return;', 'rerelease map sets')
    path.write_text(text)
    path = qc / 'client.qc'
    text = path.read_text()
    text = replace_once(text, 'void() NextLevel =\n{',
        'void() NextLevel =\n{\n    if (deathmatch) { local entity ending; gameover = TRUE; nextmap = lq_dm_next; ending = spawn(); ending.think = execute_changelevel; ending.nextthink = time + 0.1; return; }', 'DM NextLevel')
    path.write_text(text)
    for name, pairs in SINGLE_PLAYER_EDITS.items():
        path = qc / name
        text = path.read_text()
        for old, new in pairs:
            text = replace_once(text, old, new, name)
        path.write_text(text)
    with (out / 'qc-build.log').open('w') as log:
        subprocess.run([str(compiler), '-src', '.', '-Tq1'], cwd=qc, stdout=log, stderr=log, check=True, timeout=30)
    if not (qc / 'progs.dat').exists(): raise RuntimeError('Compiler did not produce progs.dat')
    destination = out / 'game/frikbot/progs.dat'
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(qc / 'progs.dat', destination)
    def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
    payloads = {'progs.dat': destination.read_bytes(), 'readme.html': (args.frikbot / 'readme.html').read_bytes()}
    notice = (args.frikbot / 'src/frikbot/bot.qc').read_text().split('This program is in the Public Domain.', 1)[1].split('======================================', 1)[0]
    notice = 'This program is in the Public Domain.' + notice
    payloads['FRIKBOT-NOTICE.txt'] = notice.encode()
    for path in sorted(args.waypoints.iterdir()) if args.waypoints.is_dir() else ():
        name = path.name.lower()
        if WAYPOINT_NAME.fullmatch(name) and path.is_file() and path.stat().st_size < 262144:
            payloads['maps/' + name] = path.read_bytes()
    if any(name.startswith('maps/') for name in payloads):
        payloads['WAYPOINTS-CREDITS.txt'] = WAYPOINT_CREDIT
    payloads['QUAKE-COPYING.txt'] = (args.base / 'qcc/COPYING').read_bytes()
    packpath = out / 'frikbot-local.pak'
    directory = bytearray()
    with packpath.open('wb') as packed:
        packed.write(b'PACK' + bytes(8))
        for name, payload in sorted(payloads.items()):
            offset = packed.tell(); packed.write(payload)
            directory.extend(struct.pack('<56sii', name.encode(), offset, len(payload)))
        offset = packed.tell(); packed.write(directory); packed.seek(4)
        packed.write(struct.pack('<ii', offset, len(directory)))
    (out / 'frikbot-build.json').write_text(json.dumps({
        'scope': 'Live Quake bot pack: id GPL QuakeC v1.01, FrikBot X 0.10.2, our QuakeC, Quake Bot Archive waypoints',
        'base_repository': 'https://github.com/id-Software/Quake-Tools',
        'base_subdirectory': BASE_PATH, 'base_version': '1.01', 'base_license': 'GPL-2.0-or-later',
        'base_pin': BASE_PIN, 'compiler_pin': COMPILER_PIN, 'compiler_sha256': sha(compiler),
        'local_pack_sha256': sha(packpath),
        'pack_entries_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
        'compiled_sources_sha256': {str(p.relative_to(qc)): sha(p) for p in sorted(qc.rglob('*.qc'))},
        'compiler_args': ['-src', '.', '-Tq1'],
        'progs_sha256': sha(destination), 'additions_sha256': sha(ROOT / 'src/quakec/live_bot.qc'),
        'frikbot_source_sha256': {p.name: sha(p) for p in sorted((args.frikbot / 'src/frikbot').glob('*.qc'))},
    }, indent=2) + '\n')

if __name__ == '__main__': main()
