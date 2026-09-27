#!/usr/bin/env python3
"""
TERMINALQUEST — SIGNAL LOST (arcade edition)

A tiny real-time arcade game. Move with arrow keys, dodge patrolling
enemies, walk into signs and locked doors — and every so often, walk
into a computer, which drops you into a real bash prompt. The first
time you reach any station it tells you exactly what to type; after
that it's a real, live prompt and whatever you type actually runs.

The ship has eight stations, one per command (ls, cat, cd, mkdir,
touch, cp, mv, rm), laid out as two rows of four rooms connected by a
shaft — up along the top, down, back along the bottom.

Run it:
    python3 terminalquest.py

Controls (overworld):
    Arrow keys    move — walk into things to interact with them
    q             quit

Controls (at a terminal):
    type          a real bash prompt, nothing is simulated
    Enter         run it
    Esc           clear the line / leave the terminal

No dependencies. Python 3 standard library only (curses is built in).
Needs a real terminal window, at least 112x28.
"""

import curses
import difflib
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections import deque

DEFAULT_ROOT = os.path.expanduser("~/TerminalQuest_Arcade")

# ==========================================================================
# Real shell execution — every command a player types actually runs this way
# ==========================================================================

DANGEROUS_PATTERNS = [
    r"\bsudo\b",
    r"\brm\b[^|;&]*\s+-\S*[rR]\S*[fF]\S*\s+/(\s|$)",
    r"\brm\b[^|;&]*\s+-\S*[fF]\S*[rR]\S*\s+/(\s|$)",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
    r"\bdd\b.*\bif=",
    r"\bmkfs\b",
]
DANGEROUS_RE = re.compile("|".join(DANGEROUS_PATTERNS))
CWD_MARK = "\x01__TQ_CWD__\x02"


OUTSIDE_MSG = "that reaches outside the game folder - stay inside it while you learn."
SAFE_DEVICES = {"/dev/null"}


def _inside(path, root):
    return path == root or path.startswith(root + os.sep)


def _reaches_outside(line, cwd, sandbox_root):
    """Best-effort guard for the accidents and jokes a learner might type:
    absolute paths, '~', '$HOME' and '..' that resolve outside the folder
    (symlinks included), and recursive `rm` aimed at the folder itself or
    anything above it. Returns a message, or None if the line looks fine.

    This is NOT a security boundary — command substitution, variables and
    scripts can still get around it (see the README's Safety boundary).
    Lines it can't parse (unbalanced quotes) are left for bash to reject.
    """
    root = os.path.realpath(sandbox_root)
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    recursive_rm = "rm" in tokens and any(
        (t.startswith("-") and not t.startswith("--") and ("r" in t or "R" in t)) or t == "--recursive"
        for t in tokens)
    for tok in tokens:
        if tok.startswith("-"):
            continue
        tok = re.sub(r"^\d*[<>]+&?", "", tok)          # 2>/dev/null, >out.txt
        if "=" in tok:                                   # FOO=/path, of=/path
            tok = tok.split("=", 1)[1]
        tok = tok.replace("${HOME}", root).replace("$HOME", root)
        if tok == "~" or tok.startswith("~/"):
            tok = root + tok[1:]
        if not tok or tok in SAFE_DEVICES:
            continue
        real = os.path.realpath(os.path.join(cwd, tok))
        if not _inside(real, root):
            return OUTSIDE_MSG
        if recursive_rm and (real == root or root.startswith(real + os.sep)):
            return "nope. not even here."
    return None


def run_command(line, cwd, sandbox_root):
    """Execute `line` as real bash, starting in `cwd`. The tracked working
    directory is snapped back if it would leave sandbox_root — this is NOT an
    OS-level sandbox; bash itself can still touch any path it has access to.
    Returns (new_cwd, out, err)."""
    stripped = line.strip()
    if not stripped:
        return cwd, "", ""
    if DANGEROUS_RE.search(stripped):
        return cwd, "", "nope. not even here."
    blocked = _reaches_outside(stripped, cwd, sandbox_root)
    if blocked:
        return cwd, "", blocked

    script = (
        f"cd {shlex.quote(cwd)} 2>/dev/null && {stripped}\n"
        f"printf '{CWD_MARK}%s' \"$(pwd -P 2>/dev/null)\"\n"
    )
    try:
        # HOME points at the game folder, so `cd`, `~` and `$HOME` land there
        # instead of on the player's real home directory.
        env = dict(os.environ, HOME=os.path.realpath(sandbox_root))
        proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=10, env=env)
    except subprocess.TimeoutExpired:
        return cwd, "", "(that took too long and was stopped)"

    out, err = proc.stdout, proc.stderr
    new_cwd = cwd
    if CWD_MARK in out:
        idx = out.rfind(CWD_MARK)
        new_cwd = out[idx + len(CWD_MARK):].strip() or cwd
        out = out[:idx]

    real_root = os.path.realpath(sandbox_root)
    real_new = os.path.realpath(new_cwd)
    if not (real_new == real_root or real_new.startswith(real_root + os.sep)):
        new_cwd = cwd
    return new_cwd, out, err


def first_word(line):
    parts = line.strip().split()
    return parts[0] if parts else ""


def _write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)


# ==========================================================================
# Per-station puzzle sandboxes — tiny, self-contained real directories.
# Every station is solved by exactly ONE command. No multi-step assembly
# anywhere — walk up, type a thing, see it react, walk away.
# ==========================================================================

class Station:
    def __init__(self, id, name, root, clue, lesson, hint, explain=None):
        self.id = id
        self.name = name
        self.root = root          # absolute path, this station's own sandbox
        self.cwd = root
        self.clue = clue          # short flavor line, shown as the box header — no command names
        self.lesson = lesson      # kept for reference; the panel below builds its COMMAND /
                                   # WHAT IT DOES / SYNTAX / YOUR TASK sections from hint/explain
                                   # and the module-level COMMAND_SYNTAX/YOUR_TASK tables instead
        self.hint = hint          # the exact command that solves this station
        self.explain = explain    # (command_word, plain-English sentence) shown again after solving
        self.transcript = []      # list of (prompt_line, output_text)
        self.attempts = 0
        self.solved = False
        self.hint_shown = False
        self.fails = 0            # submissions that didn't solve this station
        self.tip = ""             # one-line coaching after repeated misses
        self.last_command = ""
        self.resolved_explain = ""

    def run(self, line):
        self.attempts += 1
        prompt = f"{os.path.relpath(self.cwd, self.root) or '.'} $ {line}"
        self.cwd, out, err = run_command(line, self.cwd, self.root)
        text = (out + ("\n" + err if err.strip() else "")).rstrip("\n")
        self.transcript.append((prompt, text))
        self.transcript = self.transcript[-8:]
        self.last_command = line
        return out, err


# Every command taught, in teaching order: look around, read, move into a
# folder, create a folder, create a file, copy, move/rename, then delete
# last — deletion is the one command here with no undo, so it's the climax,
# not the first thing you're handed.
STATION_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H"]


def _check_ls(st):
    if st.solved:
        return
    for prompt, out in st.transcript:
        if first_word(prompt.split("$", 1)[-1]) == "ls" and out.strip():
            st.solved = True


def _check_cat(st):
    if st.solved:
        return
    for prompt, out in st.transcript:
        tail = prompt.split("$", 1)[-1] if "$" in prompt else prompt
        if first_word(tail) == "cat" and "key.txt" in tail and out.strip():
            st.solved = True


def _check_cd(st):
    if st.solved:
        return
    if os.path.basename(os.path.realpath(st.cwd)) == "vault":
        st.solved = True


def _check_mkdir(st):
    if st.solved:
        return
    if os.path.isdir(os.path.join(st.root, "stash")):
        st.solved = True


def _check_touch(st):
    if st.solved:
        return
    if os.path.isfile(os.path.join(st.root, "spare.key")):
        st.solved = True


def _check_cp(st):
    if st.solved:
        return
    if os.path.isfile(os.path.join(st.root, "backup.txt")):
        st.solved = True


def _check_mv(st):
    if st.solved:
        return
    if os.path.isfile(os.path.join(st.root, "final.txt")) and not os.path.exists(
        os.path.join(st.root, "draft.txt")
    ):
        st.solved = True


def _check_rm(st):
    if st.solved:
        return
    if not os.path.exists(os.path.join(st.root, "jam.lock")):
        st.solved = True


def coach_tip(st, line):
    """One line of coaching for a station that keeps not getting solved:
    what the learner typed vs. what this door wants. Deliberately never a
    new answer — the COMMAND box already shows that — just what was off."""
    target = first_word(st.hint)
    typed = first_word(line)
    if not typed:
        return ""
    if typed == target:
        return f"tip: right command! now check the names and order — this door wants: {st.hint}"
    if difflib.get_close_matches(typed, [target], n=1, cutoff=0.5):
        return f"tip: did you mean `{target}`? that's the command this door wants."
    return f"tip: this door needs `{target}` — look at the COMMAND box above."


def near_miss_hint(sid, st):
    """For the create-something stations (D: mkdir, E: touch): the command
    ran fine but made the wrong thing. Returns a one-line nudge instead of
    silently leaving the station unsolved, or None when it doesn't apply."""
    if sid == "D" and first_word(st.last_command) == "mkdir":
        made = [n for n in os.listdir(st.root) if os.path.isdir(os.path.join(st.root, n))]
        if made and "stash" not in made:
            return "*** mkdir worked, but this mission needs a folder called stash. ***"
    if sid == "E" and first_word(st.last_command) == "touch":
        made = [n for n in os.listdir(st.root) if os.path.isfile(os.path.join(st.root, n))]
        if made and "spare.key" not in made:
            return "*** touch worked, but this mission needs a file called spare.key. ***"
    return None


STATION_META = {
    "A": dict(
        name="??", clue="This thing hums.",
        lesson="new command: ls\nlists what's in your current folder.\n\ntype:  ls\nthen press Enter.",
        hint="ls", explain=("ls", "that was ls — it lists what's in the current folder."),
        setup=lambda root: _write(os.path.join(root, "key.txt"), "found it.\n"),
        check=_check_ls,
    ),
    "B": dict(
        name="??", clue="Paper doesn't read itself.",
        lesson="new command: cat\nprints a file's contents to the screen.\n"
               "there's a file here called key.txt.\n\ntype:  cat key.txt\nthen press Enter.",
        hint="cat key.txt", explain=("cat", "that was cat — it prints a file's contents to the screen."),
        setup=lambda root: _write(os.path.join(root, "key.txt"), "the door hums once you read this.\n"),
        check=_check_cat,
    ),
    "C": dict(
        name="??", clue="Something is sealed behind a folder marked vault.",
        lesson="new command: cd\nmoves you into a folder.\n"
               "there's a folder here called vault.\n\ntype:  cd vault\nthen press Enter.",
        hint="cd vault", explain=("cd", "that was cd — it moves you into a folder."),
        setup=lambda root: _write(os.path.join(root, "vault", "prize.txt"), "you made it in.\n"),
        check=_check_cd,
    ),
    "D": dict(
        name="??", clue="This room has nowhere to put anything.",
        lesson="new command: mkdir\ncreates a new folder.\n\ntype:  mkdir stash\nthen press Enter.",
        hint="mkdir stash", explain=("mkdir", "that was mkdir — it creates a new folder."),
        setup=lambda root: None,
        check=_check_mkdir,
    ),
    "E": dict(
        name="??", clue="Nothing here yet. Maybe that's the point.",
        lesson="new command: touch\ncreates a new, empty file.\n\ntype:  touch spare.key\nthen press Enter.",
        hint="touch spare.key", explain=("touch", "that was touch — it creates a new, empty file."),
        setup=lambda root: None,
        check=_check_touch,
    ),
    "F": dict(
        name="??", clue="There's a template here. Somewhere else needs one too.",
        lesson="new command: cp\ncopies a file, without deleting the original.\n"
               "there's a file here called template.txt.\n\ntype:  cp template.txt backup.txt\nthen press Enter.",
        hint="cp template.txt backup.txt",
        explain=("cp", "that was cp — it copies a file without deleting the original."),
        setup=lambda root: _write(os.path.join(root, "template.txt"), "daily report template\n"),
        check=_check_cp,
    ),
    "G": dict(
        name="??", clue="Something here is still called 'draft'.",
        lesson="new command: mv\nmoves or renames a file.\n"
               "there's a file here called draft.txt.\n\ntype:  mv draft.txt final.txt\nthen press Enter.",
        hint="mv draft.txt final.txt", explain=("mv", "that was mv — it moves or renames a file."),
        setup=lambda root: _write(os.path.join(root, "draft.txt"), "rename me\n"),
        check=_check_mv,
    ),
    "H": dict(
        name="??", clue="Something small is jamming the gate.",
        lesson="new command: rm\ndeletes a file — no undo, so it's worth respecting.\n"
               "there's a file here called jam.lock.\n\ntype:  rm jam.lock\nthen press Enter.",
        hint="rm jam.lock", explain=("rm", "that was rm — it deletes a file. no undo, so it's worth respecting."),
        setup=lambda root: _write(os.path.join(root, "jam.lock"), "(holding the gate signal open)\n"),
        check=_check_rm,
    ),
}

# ==========================================================================
# The terminal panel's fixed reference content — COMMAND / WHAT IT DOES /
# SYNTAX / YOUR TASK, shown every visit rather than as a one-time card.
# Keyed by station id (not by command word) since a couple of stations
# share no command with each other but the panel content is per-puzzle.
# ==========================================================================

COMMAND_SYNTAX = {
    "A": "ls", "B": "cat FILE", "C": "cd FOLDER", "D": "mkdir FOLDER",
    "E": "touch FILE", "F": "cp SOURCE DESTINATION", "G": "mv SOURCE DESTINATION",
    "H": "rm FILE",
}

YOUR_TASK = {
    "A": "List what's in this room's folder.",
    "B": "Print key.txt to see what's inside.",
    "C": "Move into the vault folder.",
    "D": "Create a new folder called stash.",
    "E": "Create a new, empty file called spare.key.",
    "F": "Copy template.txt to backup.txt — keep the original.",
    "G": "Rename draft.txt to final.txt.",
    "H": "Delete jam.lock to free the gate.",
}

# What the RESULT of the right command means — shown only on request (the
# '?' key), after a command has actually been run, so it's an explanation
# of what just happened rather than another wall of text up front.
RESULT_MEANING = {
    "ls": "Each name printed is a file or folder inside this station's folder.",
    "cat": "The printed text is exactly what's stored inside the file.",
    "cd": "No output is normal — the shell just changed which folder it's in.",
    "mkdir": "No output is normal — the folder was created silently.",
    "touch": "No output is normal — the file was created (or its timestamp updated).",
    "cp": "No output is normal — the original stays put; this made a second copy.",
    "mv": "No output is normal — the old name is gone; only the new one remains.",
    "rm": "No output is normal — the file is gone. No undo.",
}


def command_does(sid):
    """'WHAT IT DOES' text, derived from the station's existing (word, sentence)
    explain pair instead of a separately-maintained string — one source of
    truth for what each command does."""
    _, sentence = STATION_META[sid]["explain"]
    return sentence.split("— ", 1)[1] if "— " in sentence else sentence


def build_stations(game_root):
    """Build every station's real puzzle folder from scratch.

    Every launch starts a fresh campaign, so the puzzle folders are wiped and
    re-created each time. The setup markers live OUTSIDE the station folders
    on purpose: a marker inside would look like a stray file to a checker.
    """
    puzzles_root = os.path.join(game_root, "puzzles")
    markers_root = os.path.join(game_root, ".setup_markers")
    # Game progress (solved stations, doors, coins) is not saved between
    # launches, so the training folders must not persist either — otherwise
    # leftover files would mark a station "solved" the game doesn't know about.
    shutil.rmtree(puzzles_root, ignore_errors=True)
    shutil.rmtree(markers_root, ignore_errors=True)
    os.makedirs(markers_root, exist_ok=True)
    stations, checkers = {}, {}
    for sid in STATION_ORDER:
        meta = STATION_META[sid]
        root = os.path.join(puzzles_root, f"station{sid}")
        os.makedirs(root, exist_ok=True)
        marker = os.path.join(markers_root, sid)
        if not os.path.exists(marker):
            if meta["setup"]:
                meta["setup"](root)
            open(marker, "w").close()
        st = Station(sid, meta["name"], root, meta["clue"], meta["lesson"], meta["hint"], meta["explain"])
        stations[sid] = st
        checkers[sid] = meta["check"]
    return {sid: (stations[sid], checkers[sid]) for sid in STATION_ORDER}


# ==========================================================================
# The overworld map — two rows of four rooms connected by a shaft:
# right along the top, down, left along the bottom. Deliberately NOT a
# single straight corridor — every door bends the path somewhere.
# ==========================================================================

ROOM_W, ROOM_H = 10, 6      # interior size of every room
CORR_LEN = 4                 # length of each horizontal corridor between rooms
SHAFT_LEN = 3                # length of the vertical connector between the two rows
N_PER_ROW = 4

TOP_Y0 = 1
TOP_Y1 = TOP_Y0 + ROOM_H - 1
SHAFT_Y0 = TOP_Y1 + 1
SHAFT_Y1 = SHAFT_Y0 + SHAFT_LEN - 1
BOT_Y0 = SHAFT_Y1 + 1
BOT_Y1 = BOT_Y0 + ROOM_H - 1
GRID_H = BOT_Y1 + 2

# Every horizontal transition picks its OWN row — top, bottom, or middle of
# the room — instead of one constant row for the whole band. Three doors in
# a row all at the same height reads as "one line you can just walk through
# without looking"; bending each gap up or down forces you to actually
# cross the room to find the next opening.
TOP_ROW_TOP = TOP_Y0 + 1
TOP_ROW_MID = TOP_Y0 + ROOM_H // 2
TOP_ROW_BOT = TOP_Y1 - 1
BOT_ROW_TOP = BOT_Y0 + 1
BOT_ROW_MID = BOT_Y0 + ROOM_H // 2
BOT_ROW_BOT = BOT_Y1 - 1

# gap 0 = A-B / E-F, gap 1 = B-C / F-G, gap 2 = C-D / G-H
TOP_TRANS_ROWS = [TOP_ROW_TOP, TOP_ROW_BOT, TOP_ROW_MID]
BOT_TRANS_ROWS = [BOT_ROW_BOT, BOT_ROW_TOP, BOT_ROW_MID]


def _room_bounds(i):
    x0 = 1 + i * (ROOM_W + CORR_LEN)
    x1 = x0 + ROOM_W - 1
    return x0, x1


GRID_W = _room_bounds(N_PER_ROW - 1)[1] + 2

# Top row, left to right: A, B, C, D. Bottom row, physically right to left
# under the shaft: E (rightmost, directly under D), F, G, H (leftmost).
TOP_ROOMS = {"A": 0, "B": 1, "C": 2, "D": 3}
BOTTOM_ROOMS = {"E": 3, "F": 2, "G": 1, "H": 0}


def _top_room_rect(sid):
    x0, x1 = _room_bounds(TOP_ROOMS[sid])
    return x0, TOP_Y0, x1, TOP_Y1


def _bottom_room_rect(sid):
    x0, x1 = _room_bounds(BOTTOM_ROOMS[sid])
    return x0, BOT_Y0, x1, BOT_Y1


def room_rect(sid):
    return _top_room_rect(sid) if sid in TOP_ROOMS else _bottom_room_rect(sid)


# Bulkhead near room H's left wall: only the final gate connects the outer
# play area to the exit chamber, so reaching EXIT_POS requires solving H.
# It sits at whatever row the G-H corridor actually arrives on.
H_X0, H_Y0, H_X1, H_Y1 = _bottom_room_rect("H")
BULKHEAD_X = H_X0 + 1
H_ARRIVAL_ROW = BOT_TRANS_ROWS[2]
EXIT_POS = (H_X0, H_ARRIVAL_ROW)

SHAFT_X = _top_room_rect("D")[2] - 2  # a column inside room D/E's shared x-range


def carve(grid, x0, y0, x1, y1, ch="."):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            grid[y][x] = ch


def build_grid():
    grid = [["#"] * GRID_W for _ in range(GRID_H)]

    for sid in STATION_ORDER:
        x0, y0, x1, y1 = room_rect(sid)
        carve(grid, x0, y0, x1, y1)

    # horizontal corridors, top row (A-B, B-C, C-D) — each gap at its own row
    for i in range(N_PER_ROW - 1):
        x1_left = _room_bounds(i)[1]
        x0_right = _room_bounds(i + 1)[0]
        row = TOP_TRANS_ROWS[i]
        carve(grid, x1_left + 1, row, x0_right - 1, row)

    # horizontal corridors, bottom row (E-F, F-G, G-H) — same x geometry
    for i in range(N_PER_ROW - 1):
        x1_left = _room_bounds(i)[1]
        x0_right = _room_bounds(i + 1)[0]
        row = BOT_TRANS_ROWS[i]
        carve(grid, x1_left + 1, row, x0_right - 1, row)

    # the shaft connecting D (top) down to E (bottom)
    carve(grid, SHAFT_X, TOP_Y1 + 1, SHAFT_X, BOT_Y0 - 1)

    # bulkhead sealing off the exit chamber inside room H — a solid wall with
    # exactly one opening, at the gate. The gate tile itself stays real floor;
    # it's the "gate" entry in DOORS (below) that actually blocks it while locked.
    carve(grid, BULKHEAD_X, H_Y0, BULKHEAD_X, H_Y1, "#")
    grid[H_ARRIVAL_ROW][BULKHEAD_X] = "."

    # a sealed secret closet off room B's corridor, revealed once B is solved
    for cell in HIDDEN_CLOSET_CELLS:
        grid[cell[1]][cell[0]] = "#"

    return grid


PLAYER_START = (_top_room_rect("A")[0] + 1, TOP_TRANS_ROWS[0])

DOORS = {
    "doorAB": {"pos": (_room_bounds(0)[1] + 1 + CORR_LEN // 2, TOP_TRANS_ROWS[0]), "station": "A"},
    "doorBC": {"pos": (_room_bounds(1)[1] + 1 + CORR_LEN // 2, TOP_TRANS_ROWS[1]), "station": "B"},
    "doorCD": {"pos": (_room_bounds(2)[1] + 1 + CORR_LEN // 2, TOP_TRANS_ROWS[2]), "station": "C"},
    "hatch":  {"pos": (SHAFT_X, SHAFT_Y0 + SHAFT_LEN // 2), "station": "D"},
    "doorEF": {"pos": (_room_bounds(3)[0] - 1 - CORR_LEN // 2, BOT_TRANS_ROWS[0]), "station": "E"},
    "doorFG": {"pos": (_room_bounds(2)[0] - 1 - CORR_LEN // 2, BOT_TRANS_ROWS[1]), "station": "F"},
    "doorGH": {"pos": (_room_bounds(1)[0] - 1 - CORR_LEN // 2, BOT_TRANS_ROWS[2]), "station": "G"},
    "gate":   {"pos": (BULKHEAD_X, H_ARRIVAL_ROW), "station": "H"},
}

STATION_TO_DOOR = {d["station"]: name for name, d in DOORS.items()}


def _computer_pos(sid):
    x0, y0, x1, y1 = room_rect(sid)
    row = y0 + 2 if sid in ("A", "C", "E", "G") else y1 - 2
    return (min(x1 - 1, x0 + 6), row)


COMPUTERS = {_computer_pos(sid): sid for sid in STATION_ORDER}

HIDDEN_CLOSET_CELLS = [(_room_bounds(1)[1] + 2, TOP_ROW_MID)]

SIGNS = {
    (PLAYER_START[0] + 2, TOP_Y0 + 1): "signal's out somewhere on this ship. eight stations, one signal.",
    (H_X1 - 1, BOT_Y0 + 1): "almost there. don't rush the last one.",
}

def _enemy(sid, row_offset, period, direction):
    x0, y0, x1, y1 = room_rect(sid)
    return {"x": x0 + 2, "y": y0 + row_offset, "min": x0 + 1, "max": x1 - 1,
            "dir": direction, "period": period, "counter": 0}


# Each room gets its own row, picked from whatever's left over once that
# room's door(s), computer, and coin have claimed theirs — never row 0 for
# everyone. That keeps a patrol from ever blocking the one path through or
# sitting on top of another object, and stops every enemy in the section
# from lining up on the same horizontal band.
ENEMIES_INIT = [
    _enemy("A", 4, 4, 1),
    _enemy("B", 2, 3, -1),
    _enemy("C", 5, 4, 1),
    _enemy("D", 0, 3, -1),
    _enemy("F", 4, 4, 1),
    _enemy("G", 1, 3, -1),
]

# One required coin in each top-band room (A-D) — level 1 won't open the
# way down until all four are found, so "clear this level" means the whole
# room, not just its terminal. The hidden closet coin is a bonus and does
# NOT count toward that — finding it is optional either way.
BAND0_COINS = [
    (room_rect("A")[0] + 6, room_rect("A")[1] + 1),
    (room_rect("B")[0] + 6, room_rect("B")[3] - 1),
    (room_rect("C")[0] + 3, room_rect("C")[3] - 1),
    (room_rect("D")[0] + 3, room_rect("D")[1] + 1),
]

# Same idea, one level in: level 2 is E and F. Both required coins here
# already existed as optional bonus coins in earlier versions of the map —
# this just makes them mandatory for the level, same as BAND0_COINS did.
BAND1_COINS = [
    (room_rect("E")[0] + 2, room_rect("E")[3] - 1),
    (room_rect("F")[0] + 6, room_rect("F")[1] + 1),
]

COINS_INIT = BAND0_COINS + BAND1_COINS + [
    (room_rect("H")[2] - 1, room_rect("H")[1] + 1),
    HIDDEN_CLOSET_CELLS[0],
]

# Three levels: clear level 1 (A-D) to reveal level 2 (E,F); clear level 2
# to reveal level 3 (G,H). Each level's room(s) go back under fog once
# you've moved past them — "cleared" doesn't mean "still on screen."
STAGES = [
    {"num": 1, "gate_station": "D", "gate_door": "hatch", "coins": BAND0_COINS},
    {"num": 2, "gate_station": "F", "gate_door": "doorFG", "coins": BAND1_COINS},
    {"num": 3, "gate_station": "H", "gate_door": None, "coins": []},
]
GATE_STATIONS = {s["gate_station"] for s in STAGES if s["gate_door"]}

LOCKED_LINES = ["Locked.", "Won't budge.", "Sealed tight.", "Nope."]


# ==========================================================================
# BFS route-finding — a small utility used by tests (and the game's own
# --selftest) so a map edit here never leaves a test full of stale,
# hand-counted step numbers somewhere else.
# ==========================================================================

def bfs_route(game, target):
    """Shortest sequence of (dx, dy) moves from the player's current tile to
    target, treating walls and currently-locked doors as impassable."""
    start = (game.px, game.py)
    if start == target:
        return []

    def passable(x, y):
        if game.wall(x, y):
            return False
        d = game.door_at(x, y)
        if d and d["locked"]:
            return False
        # Computers, signs, and the exit all pull the player out of free
        # movement the instant they're stepped on. That's fine as the final
        # destination, but routing *through* one mid-path would strand the
        # walk early — so treat them as passable only when they're the target.
        if (x, y) != target and ((x, y) in game.computers or (x, y) in game.signs or (x, y) == EXIT_POS):
            return False
        return True

    q = deque([start])
    prev = {start: None}
    while q:
        cur = q.popleft()
        if cur == target:
            break
        cx, cy = cur
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cx + dx, cy + dy
            if (nx, ny) in prev or not (0 <= nx < GRID_W and 0 <= ny < GRID_H):
                continue
            if not passable(nx, ny):
                continue
            prev[(nx, ny)] = cur
            q.append((nx, ny))
    if target not in prev:
        return None
    path, cur = [], target
    while prev[cur] is not None:
        px, py = prev[cur]
        cx, cy = cur
        path.append((cx - px, cy - py))
        cur = (px, py)
    path.reverse()
    return path


def walk_to(game, target):
    """Drive the player from where it is to target via try_move. Stops early
    if a move changes the game mode (e.g. stepping onto a computer/sign), and
    re-plans if an enemy hit respawns the player mid-route."""
    for _ in range(6):
        moves = bfs_route(game, target)
        assert moves is not None, f"no path from {(game.px, game.py)} to {target}"
        x, y = game.px, game.py
        for dx, dy in moves:
            if game.mode != "overworld":
                return
            game.try_move(dx, dy)
            x, y = x + dx, y + dy
            if (game.px, game.py) != (x, y) and game.mode == "overworld":
                break  # blocked or respawned — plan again from here
        else:
            return
        if (game.px, game.py) == target:
            return


def collect_all_coins(game):
    """Test/selftest helper: walk to every coin still on the map."""
    for pos in COINS_INIT:
        while game.coins[pos]:
            game.dismiss_sign()
            game.dismiss_congrats()
            walk_to(game, pos)
    game.dismiss_sign()
    game.dismiss_congrats()


# ==========================================================================
# Optional end-of-level quiz. Three questions drawn at random from a pool
# for each level. "Type it" questions are graded by running the learner's
# answer as REAL bash in a throwaway folder and checking the result (so any
# command that gets the right outcome counts); "choice" questions are
# pick-a-number. First wrong answer -> a hint; second wrong -> the answer is
# shown and the quiz moves on. Never costs a life, never blocks progress.
# ==========================================================================

QUIZ_LEN = 3


def _type_q(prompt, hint, answer, check, files=None, dirs=None):
    return dict(kind="type", prompt=prompt, hint=hint, answer=answer,
                check=check, files=files or {}, dirs=dirs or [])


def _choice_q(prompt, hint, options, correct, why):
    """`correct` is the index in `options` of the right answer (options are
    shuffled per draw, so the right one isn't always in the same slot)."""
    return dict(kind="choice", prompt=prompt, hint=hint, options=options,
                correct=correct, why=why, answer=options[correct])


def _has(tmp, *names):
    return all(os.path.exists(os.path.join(tmp, n)) for n in names)


def _read(tmp, name):
    try:
        with open(os.path.join(tmp, name)) as f:
            return f.read()
    except OSError:
        return None


QUIZ_POOLS = {
    1: [  # ls, cat, cd, mkdir
        _type_q("List the files in this folder.", "It's a two-letter command: l, s.", "ls",
                lambda tmp, cwd, out: "apple.txt" in out and "pear.txt" in out,
                files={"apple.txt": "a\n", "pear.txt": "p\n"}),
        _type_q("Print what's written inside notes.txt.", "Use the command that prints a file: cat FILE.",
                "cat notes.txt", lambda tmp, cwd, out: "remember the milk" in out,
                files={"notes.txt": "remember the milk\n"}),
        _type_q("Move into the folder called projects.", "cd means 'change directory': cd FOLDER.",
                "cd projects",
                lambda tmp, cwd, out: os.path.basename(cwd) == "projects", dirs=["projects"]),
        _type_q("Create a new folder called photos.", "mkdir means 'make directory': mkdir NAME.",
                "mkdir photos", lambda tmp, cwd, out: os.path.isdir(os.path.join(tmp, "photos"))),
        _choice_q("Which command shows what's inside a file?", "One of these prints a file's contents.",
                  ["ls", "cat", "cd", "mkdir"], 1, "cat prints a file's contents to the screen."),
        _choice_q("Which command creates a new folder?", "Its name means 'make directory'.",
                  ["mkdir", "cd", "ls", "cat"], 0, "mkdir makes a new folder."),
        _choice_q("You type `cd vault` and nothing is printed. What happened?",
                  "In the shell, no news is often good news.",
                  ["It failed", "It worked: you're now inside vault", "vault is empty", "vault was deleted"], 1,
                  "cd is silent when it works; it just changes which folder you're in."),
        _choice_q("`cat missing.txt` prints 'No such file or directory'. What does that mean?",
                  "Read the message: which thing can't bash find?",
                  ["The file isn't in this folder", "cat is broken", "The file is empty", "You need admin rights"], 0,
                  "bash couldn't find a file with that name in the current folder. Check the spelling with ls."),
    ],
    2: [  # touch, cp (+ review)
        _type_q("Create a new, empty file called todo.txt.", "touch FILE creates an empty file.",
                "touch todo.txt", lambda tmp, cwd, out: os.path.isfile(os.path.join(tmp, "todo.txt"))),
        _type_q("Create a new, empty file called readme.md.", "touch FILE creates an empty file.",
                "touch readme.md", lambda tmp, cwd, out: os.path.isfile(os.path.join(tmp, "readme.md"))),
        _type_q("Copy report.txt to a new file called report-backup.txt.", "cp SOURCE DESTINATION.",
                "cp report.txt report-backup.txt",
                lambda tmp, cwd, out: _read(tmp, "report-backup.txt") == "q3 numbers\n" and _has(tmp, "report.txt"),
                files={"report.txt": "q3 numbers\n"}),
        _choice_q("Which command makes a new, empty file?", "It's not mkdir; that one makes folders.",
                  ["touch", "cat", "cp", "ls"], 0, "touch creates an empty file if it doesn't exist."),
        _choice_q("After `cp a.txt b.txt`, how many files do you have?", "Does cp remove the original?",
                  ["1 (a.txt is gone)", "2 (a.txt and b.txt)", "1 (only b.txt)", "0"], 1,
                  "cp copies: the original stays and you get a second file."),
        _choice_q("In `cp template.txt backup.txt`, which name is the NEW file?",
                  "Source first, destination second.",
                  ["template.txt", "backup.txt", "both", "neither"], 1,
                  "The first name is what you copy from; the second is the copy being made."),
        _type_q("Review: create a folder called backups.", "mkdir NAME.", "mkdir backups",
                lambda tmp, cwd, out: os.path.isdir(os.path.join(tmp, "backups"))),
        _choice_q("Review: which command lists the files in a folder?", "It's the two-letter one.",
                  ["cat", "ls", "cd", "touch"], 1, "ls lists what's in the current folder."),
    ],
    3: [  # mv, rm (+ review)
        _type_q("Rename draft.txt to final.txt.", "mv OLD NEW renames a file.", "mv draft.txt final.txt",
                lambda tmp, cwd, out: _has(tmp, "final.txt") and not _has(tmp, "draft.txt"),
                files={"draft.txt": "d\n"}),
        _type_q("Move photo.jpg into the folder called archive.", "mv FILE FOLDER moves it inside.",
                "mv photo.jpg archive",
                lambda tmp, cwd, out: _has(tmp, "archive/photo.jpg") and not _has(tmp, "photo.jpg"),
                files={"photo.jpg": "img\n"}, dirs=["archive"]),
        _type_q("Delete the file junk.tmp.", "rm FILE deletes it, with no undo.", "rm junk.tmp",
                lambda tmp, cwd, out: not _has(tmp, "junk.tmp"), files={"junk.tmp": "x\n"}),
        _choice_q("Which command deletes a file with no way to undo it?", "It's the dangerous one.",
                  ["rm", "mv", "cp", "touch"], 0, "rm permanently deletes; there's no trash can."),
        _choice_q("What does `mv old.txt new.txt` do?", "mv also means 'rename'.",
                  ["Copies it", "Renames old.txt to new.txt", "Deletes both", "Prints it"], 1,
                  "mv moves or renames: old.txt is gone and new.txt holds its contents."),
        _choice_q("What's the difference between cp and mv?", "Think about what's left behind.",
                  ["cp keeps the original; mv doesn't", "No difference", "mv keeps the original; cp doesn't",
                   "cp deletes files"], 0,
                  "cp makes a copy and leaves the original; mv moves it so the original name is gone."),
        _type_q("Review: copy a.txt to b.txt.", "cp SOURCE DESTINATION.", "cp a.txt b.txt",
                lambda tmp, cwd, out: _has(tmp, "a.txt", "b.txt"), files={"a.txt": "a\n"}),
        _type_q("Review: create an empty file called done.txt.", "touch FILE.", "touch done.txt",
                lambda tmp, cwd, out: _has(tmp, "done.txt")),
    ],
}


def grade_typed(question, line):
    """Run `line` as real bash in a throwaway folder set up for `question`.
    Returns (ok, first_line_of_bash_error_or_empty)."""
    tmp = tempfile.mkdtemp(prefix="tq_quiz_")
    try:
        for d in question["dirs"]:
            os.makedirs(os.path.join(tmp, d), exist_ok=True)
        for name, content in question["files"].items():
            _write(os.path.join(tmp, name), content)
        cwd, out, err = run_command(line, tmp, tmp)
        ok = bool(question["check"](tmp, cwd, out))
        problem = ""
        if not ok and err.strip():
            problem = next((ln for ln in err.splitlines() if ln.strip()), "")
        return ok, problem
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# Game state — pure logic, no curses. Testable on its own.
# ==========================================================================

class Game:
    def __init__(self, root, reset=False, quiz=True, rng=None, enemies=True):
        self.root = root
        self.enemies_on = enemies   # 'learn mode' turns patrols off ('e' in game)
        self.sound_on = True        # terminal bell when a station is solved ('b')
        self.bell = False           # set by the game, consumed by main()
        self.quiz_enabled = quiz
        self.rng = rng or random.Random()
        self.quiz = None            # active quiz session (see begin_quiz_offer)
        self.quiz_taken = 0         # how many questions were attempted / right,
        self.quiz_right = 0         # across every quiz taken this run
        if reset and os.path.isdir(root):
            shutil.rmtree(root)
        os.makedirs(root, exist_ok=True)

        self.grid = build_grid()
        self.doors = {k: dict(v, locked=True) for k, v in DOORS.items()}
        self.computers = dict(COMPUTERS)
        self.signs = dict(SIGNS)
        self.enemies = [dict(e) for e in ENEMIES_INIT]
        self.coins = {pos: True for pos in COINS_INIT}
        self.hidden_revealed = False

        stations = build_stations(root)
        self.stations = {k: v[0] for k, v in stations.items()}
        self._checkers = {k: v[1] for k, v in stations.items()}

        self.px, self.py = PLAYER_START
        self.facing = "right"
        self.lives = 3
        self.score = 0
        self.invuln = 0
        self.tick_count = 0

        self.mode = "overworld"     # overworld | quitconfirm | sign | terminal | congrats | quizoffer | quiz | quizdone | gameover | win
        self.stage = 1                   # which of the 3 levels is currently the visible one
        self.congrats_text = ""
        self.congrats_level = 0
        self.learned = []           # plain-English recaps, in the order you earned them
        self.sign_text = ""
        self.active_station = None
        self.input_buf = ""
        self.return_timer = 0
        self.high_contrast = False       # toggled with 't' — bold/reverse/underline, no color
        self.show_explanation = False    # toggled with '?' — reveals the "why" for the last result
        self.message = ""
        self.flash = 0

    # -- helpers -----------------------------------------------------
    def wall(self, x, y):
        if x < 0 or y < 0 or x >= GRID_W or y >= GRID_H:
            return True
        return self.grid[y][x] == "#"

    def door_at(self, x, y):
        for d in self.doors.values():
            if d["pos"] == (x, y):
                return d
        return None

    def enemy_at(self, x, y):
        if not self.enemies_on:
            return None
        for e in self.enemies:
            if (e["x"], e["y"]) == (x, y):
                return e
        return None

    # -- overworld tick (enemy movement) ------------------------------
    def tick(self):
        if self.mode != "overworld":
            return
        self.tick_count += 1
        if self.invuln > 0:
            self.invuln -= 1
        if self.flash > 0:
            self.flash -= 1

        for e in self.enemies if self.enemies_on else ():
            e["counter"] += 1
            if e["counter"] < e["period"]:
                continue
            e["counter"] = 0
            nx = e["x"] + e["dir"]
            if nx < e["min"] or nx > e["max"] or self.wall(nx, e["y"]):
                e["dir"] *= -1
                nx = e["x"] + e["dir"]
            e["x"] = nx
            if (e["x"], e["y"]) == (self.px, self.py):
                self._hit()

    def _hit(self):
        if self.invuln > 0:
            return
        self.lives -= 1
        self.message = "Ouch!"
        self.flash = 6
        if self.lives <= 0:
            self.mode = "gameover"
        else:
            self.px, self.py = PLAYER_START
            self.invuln = 10

    # -- movement ------------------------------------------------------
    def try_move(self, dx, dy):
        if self.mode != "overworld":
            return
        self.facing = {(0, -1): "up", (0, 1): "down", (-1, 0): "left", (1, 0): "right"}.get(
            (dx, dy), self.facing
        )
        nx, ny = self.px + dx, self.py + dy

        if self.wall(nx, ny):
            return

        door = self.door_at(nx, ny)
        if door and door["locked"]:
            self.message = random.choice(LOCKED_LINES)
            self.flash = 3
            return

        if (nx, ny) == EXIT_POS:
            left = self.coins_remaining()
            if left:
                # Stay outside the exit: the signal isn't fully recovered yet.
                self.mode = "sign"
                self.sign_text = (
                    "SIGNAL INCOMPLETE\n\n"
                    f"{left} of {len(COINS_INIT)} signal coins still uncollected.\n"
                    "Every coin must be recovered before you can leave.\n\n"
                    "Some are hidden — check the side rooms and sealed closets."
                )
                return

        if self.enemy_at(nx, ny) is not None and self.invuln == 0:
            self.px, self.py = nx, ny
            self._hit()
            return

        self.px, self.py = nx, ny

        if (nx, ny) in self.coins and self.coins[(nx, ny)]:
            self.coins[(nx, ny)] = False
            self.score += 1
            self.message = "+1"
            self._maybe_advance_stage()

        if (nx, ny) in self.signs:
            self.mode = "sign"
            self.sign_text = self.signs[(nx, ny)]
            return

        if (nx, ny) in self.computers:
            self.enter_terminal(self.computers[(nx, ny)])
            return

        if (nx, ny) == EXIT_POS:
            self._finish_run()
            return

    def coins_remaining(self):
        return sum(1 for still_there in self.coins.values() if still_there)

    def dismiss_sign(self):
        if self.mode == "sign":
            self.mode = "overworld"

    def _finish_run(self):
        """Reached the exit with every coin: level 3's optional quiz, then win."""
        if self.quiz_enabled:
            self.begin_quiz_offer(3, "win")
        else:
            self._enter_mode("win")

    def _enter_mode(self, mode):
        self.mode = mode
        if mode == "win":
            self.stage = len(STAGES) + 1  # every level reads as cleared/sealed now

    def dismiss_congrats(self):
        if self.mode == "congrats":
            if self.quiz_enabled and self.congrats_level in QUIZ_POOLS:
                self.begin_quiz_offer(self.congrats_level, "overworld")
            else:
                self.mode = "overworld"

    # -- optional end-of-level quiz ---------------------------------------
    def begin_quiz_offer(self, level, then):
        """Ask whether the player wants a quick 3-question recap of `level`.
        `then` is the mode to enter afterwards ("overworld" or "win")."""
        self.quiz = dict(level=level, then=then, items=[], idx=0, attempts=0,
                         phase="asking", lines=[], right=0)
        self.input_buf = ""
        self.mode = "quizoffer"

    def quiz_skip(self):
        """Decline the offer, or bail out mid-quiz (Esc)."""
        if self.quiz is None:
            return
        then = self.quiz["then"]
        self.quiz = None
        self.input_buf = ""
        self._enter_mode(then)

    def quiz_start(self):
        if self.mode != "quizoffer" or self.quiz is None:
            return
        pool = QUIZ_POOLS[self.quiz["level"]]
        items = []
        for q in self.rng.sample(pool, QUIZ_LEN):
            item = dict(q)
            if q["kind"] == "choice":
                order = list(range(len(q["options"])))
                self.rng.shuffle(order)
                item["options"] = [q["options"][i] for i in order]
                item["correct"] = order.index(q["correct"])
            items.append(item)
        self.quiz.update(items=items, idx=0, attempts=0, phase="asking", lines=[], right=0)
        self.input_buf = ""
        self.mode = "quiz"

    def quiz_question(self):
        return self.quiz["items"][self.quiz["idx"]]

    def quiz_char(self, ch):
        if self.mode == "quiz" and self.quiz["phase"] == "asking" and len(self.input_buf) < 60:
            self.input_buf += ch

    def quiz_backspace(self):
        self.input_buf = self.input_buf[:-1]

    def quiz_submit(self, answer=None):
        """Grade the answer: the typed line (input_buf) for "type" questions,
        or the chosen option index for "choice" questions."""
        if self.mode != "quiz" or self.quiz["phase"] != "asking":
            return
        q, z = self.quiz_question(), self.quiz
        if q["kind"] == "type":
            line, self.input_buf = self.input_buf, ""
            if not line.strip():
                return
            ok, problem = grade_typed(q, line)
        else:
            if answer is None or not (0 <= answer < len(q["options"])):
                return
            ok, problem = answer == q["correct"], ""
        if ok:
            z["right"] += 1
            self.quiz_right += 1
            self._quiz_feedback(["Correct!"] + ([q["why"]] if q.get("why") else []))
            return
        z["attempts"] += 1
        if z["attempts"] == 1:
            z["lines"] = ([f"bash said: {problem}"] if problem else []) + [f"Not quite. Hint: {q['hint']}", "Try once more."]
        else:
            self._quiz_feedback([f"The answer: {q['answer']}"] + ([q["why"]] if q.get("why") else []))

    def _quiz_feedback(self, lines):
        z = self.quiz
        z["phase"] = "feedback"
        z["lines"] = lines
        self.quiz_taken += 1

    def quiz_continue(self):
        if self.mode != "quiz" or self.quiz["phase"] != "feedback":
            return
        z = self.quiz
        z["idx"] += 1
        z["attempts"], z["phase"], z["lines"] = 0, "asking", []
        self.input_buf = ""
        if z["idx"] >= len(z["items"]):
            z["phase"] = "summary"
            self.mode = "quizdone"

    def quiz_finish(self):
        if self.mode == "quizdone":
            self.quiz_skip()

    # -- terminal mode ---------------------------------------------------
    def enter_terminal(self, station_id):
        self.mode = "terminal"
        self.active_station = station_id
        self.input_buf = ""
        self.show_explanation = False
        st = self.stations[station_id]
        self.return_timer = 15 if st.solved else 0

    def terminal_suggest(self):
        """Tab: load the station's exact command into the prompt WITHOUT
        running it — the command is already shown in the panel above, so
        this is a discoverability aid the player can still edit, not an
        auto-solve."""
        st = self.stations[self.active_station]
        if not st.solved:
            self.input_buf = st.hint

    def toggle_explanation(self):
        st = self.stations[self.active_station]
        if st.transcript:
            self.show_explanation = not self.show_explanation

    def terminal_char(self, ch):
        if len(self.input_buf) < 60:
            self.input_buf += ch

    def terminal_backspace(self):
        self.input_buf = self.input_buf[:-1]

    def terminal_escape(self):
        if self.input_buf:
            self.input_buf = ""
        else:
            self.exit_terminal()

    def exit_terminal(self):
        self.mode = "overworld"
        self.active_station = None
        self.input_buf = ""
        self.return_timer = 0
        self.show_explanation = False

    def terminal_submit(self):
        st = self.stations[self.active_station]
        line = self.input_buf
        self.input_buf = ""
        if not line.strip():
            return
        self.show_explanation = False
        st.run(line)
        was_solved = st.solved
        self._checkers[self.active_station](st)
        st.tip = ""
        if st.solved and not was_solved:
            self.bell = True
            self._apply_effect(self.active_station)
            word, sentence = st.explain
            st.resolved_explain = sentence
            if word and word not in self.learned:
                self.learned.append(word)
            gate = next((s for s in STAGES if s["gate_station"] == self.active_station), None)
            if gate and gate["gate_door"] and self.doors[gate["gate_door"]]["locked"]:
                st.transcript.append(("", "*** the way on stays sealed — something's still uncollected up here. ***"))
            else:
                st.transcript.append(("", "*** something unlocks, somewhere. ***"))
            self.return_timer = 20
        elif not st.solved:
            st.fails += 1
            nudge = near_miss_hint(self.active_station, st)
            if nudge:
                st.transcript.append(("", nudge))
            elif st.fails >= 2:
                st.tip = coach_tip(st, line)

    def tick_terminal(self):
        if self.mode == "terminal" and self.return_timer > 0:
            self.return_timer -= 1
            if self.return_timer == 0:
                self.exit_terminal()

    def _apply_effect(self, station_id):
        if station_id in GATE_STATIONS:
            self._maybe_advance_stage()
        else:
            self.doors[STATION_TO_DOOR[station_id]]["locked"] = False
        if station_id == "B":
            for cell in HIDDEN_CLOSET_CELLS:
                self.grid[cell[1]][cell[0]] = "."
            self.hidden_revealed = True

    def _maybe_advance_stage(self):
        """A level's gate opens only once its last station is solved AND
        every one of its required coins is collected — clearing a level
        means clearing the whole thing, not just its last terminal. Once a
        level clears, the game moves on to the next one: its gate opens, a
        congrats beat plays, and (via the fog logic in draw_base) the next
        level unmasks. The level just cleared stays visible for good — only
        levels still ahead of you ever render as a sealed block."""
        if self.stage > len(STAGES):
            return
        cur = STAGES[self.stage - 1]
        if not cur["gate_door"]:
            return
        if self.stations[cur["gate_station"]].solved and all(
            not self.coins.get(p, False) for p in cur["coins"]
        ):
            self.doors[cur["gate_door"]]["locked"] = False
            self.stage += 1
            self.mode = "congrats"
            self.congrats_text = f"LEVEL {cur['num']} COMPLETE!\n\nWell played."
            self.congrats_level = cur["num"]

    # -- lifecycle -------------------------------------------------------
    def retry(self):
        self.lives = 3
        self.px, self.py = PLAYER_START
        self.invuln = 10
        self.mode = "overworld"

    def next_station(self):
        """Id of the first station not yet solved, or None when all are."""
        return next((sid for sid in STATION_ORDER if not self.stations[sid].solved), None)

    def toggle_enemies(self):
        self.enemies_on = not self.enemies_on
        self.message = "ENEMIES OFF (learn mode)" if not self.enemies_on else "ENEMIES ON"

    def toggle_sound(self):
        self.sound_on = not self.sound_on
        self.message = "SOUND ON" if self.sound_on else "SOUND OFF"

    def ask_quit(self):
        if self.mode == "overworld":
            self.mode = "quitconfirm"

    def cancel_quit(self):
        if self.mode == "quitconfirm":
            self.mode = "overworld"

    def solved_count(self):
        return sum(1 for s in self.stations.values() if s.solved)


# ==========================================================================
# curses front-end
# ==========================================================================
#
# Color is meaningful, not decorative: the player, the exit, and every
# success state all share one color, so it always means "you / good."
# Danger is red. Locked doors are yellow. Terminals get the one cool color,
# blue. Coins get no color at all: the least important thing here shouldn't
# compete for attention.
#
# These are deliberately the terminal's own standard 8 colors, not custom
# 256-color shades — a terminal's built-in palette is the one thing that's
# actually calibrated for readability against *that* terminal's background,
# light or dark.

CP_PLAYER = CP_ENEMY = CP_DOOR = CP_COMP = CP_COIN = CP_OK = CP_ERR = 0


def setup_colors(high_contrast=False):
    """high_contrast=True drops color entirely in favor of bold/reverse/
    underline — attributes every terminal renders distinctly regardless of
    its color palette or a player's color vision, for setups where the
    default colors don't read well."""
    global CP_PLAYER, CP_ENEMY, CP_DOOR, CP_COMP, CP_COIN, CP_OK, CP_ERR
    CP_PLAYER = CP_ENEMY = CP_DOOR = CP_COMP = CP_COIN = CP_OK = CP_ERR = 0
    if high_contrast:
        CP_PLAYER = curses.A_BOLD | curses.A_REVERSE
        CP_ENEMY = curses.A_BOLD | curses.A_UNDERLINE | curses.A_REVERSE
        CP_DOOR = curses.A_BOLD | curses.A_REVERSE
        CP_COMP = curses.A_BOLD | curses.A_UNDERLINE
        CP_COIN = curses.A_BOLD
        CP_OK = curses.A_BOLD | curses.A_REVERSE
        CP_ERR = curses.A_BOLD | curses.A_UNDERLINE | curses.A_REVERSE
        return
    if not curses.has_colors():
        return
    try:
        curses.init_pair(1, curses.COLOR_MAGENTA, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_BLUE, -1)
        CP_PLAYER = curses.color_pair(1) | curses.A_BOLD
        CP_ENEMY = curses.color_pair(2) | curses.A_BOLD
        CP_DOOR = curses.color_pair(3) | curses.A_BOLD
        CP_COMP = curses.color_pair(4) | curses.A_BOLD
        CP_COIN = 0                                    # deliberately uncolored
        CP_OK = curses.color_pair(1) | curses.A_BOLD    # same family as "you"
        CP_ERR = curses.color_pair(2) | curses.A_BOLD
    except curses.error:
        pass


FACING_GLYPH = {"up": "▲", "down": "▼", "left": "◀", "right": "▶"}

WALL_GLYPH = "█"
CELL_W = 2  # each map tile is drawn 2 terminal columns wide — makes it read as blocky/square
LEGEND = "[▶] you    x enemy    ▓ locked door    ▣A terminal    * coin    X exit"


def tile_glyph(game, x, y):
    """Pure, testable: what belongs at (x, y) right now — a wall, floor, or
    one of coin/computer/sign/door/exit. Kept independent of curses so it
    can be unit-tested directly.

    The wall check comes FIRST, on purpose: a coin, computer, or anything
    else sitting on a currently-sealed tile (like the secret closet before
    its wall opens) must never show through. Content only renders once the
    tile it's on is genuinely open floor.
    """
    if game.grid[y][x] == "#":
        return WALL_GLYPH, "wall"
    if (x, y) in game.coins and game.coins[(x, y)]:
        return "*", "coin"
    if (x, y) in game.computers:
        return "▣", "computer"
    if (x, y) in game.signs:
        return "!", "sign"
    d = game.door_at(x, y)
    if d:
        return ("▓", "door_locked") if d["locked"] else ("'", "door_unlocked")
    if (x, y) == EXIT_POS:
        return "X", "exit"
    return ".", "floor"


_TILE_STYLE_KEYS = {
    "wall": lambda: curses.A_DIM,
    "coin": lambda: CP_COIN | curses.A_BOLD,
    "computer": lambda: CP_COMP | curses.A_BOLD,
    "sign": lambda: curses.A_BOLD,
    "door_locked": lambda: CP_DOOR | curses.A_BOLD,
    "door_unlocked": lambda: CP_DOOR | curses.A_DIM,
    "exit": lambda: CP_OK | curses.A_BOLD,
    "floor": lambda: curses.A_DIM,
}


def _zone_for(x, y):
    """Which of the 3 levels a tile belongs to (1, 2, or 3), or None for the
    always-visible shaft/corridor connecting the two bands — that stays on
    screen regardless of level so the path itself is never the thing that's
    fogged, only the rooms."""
    if y <= TOP_Y1:
        return 1
    if y < BOT_Y0:
        return None
    boundary_x = _room_bounds(2)[0]  # E/F start here; G/H sit to its left
    return 2 if x >= boundary_x else 3


def _zone_banners(game):
    """(text, row, x0_grid, x1_grid) for whatever part of the bottom band
    hasn't been reached yet. A level you've already cleared stays revealed
    for good — level 1 is visible from the very first frame and never goes
    back under fog, so it never gets a banner at all. While NEITHER level 2
    nor level 3 has been reached yet, the whole bottom band reads as one
    sealed area with a single banner (two separate labels side by side read
    as a rendering glitch, not a bigger locked room)."""
    boundary_x = _room_bounds(2)[0]
    row = BOT_Y0 + (BOT_Y1 - BOT_Y0) // 2
    zone2_sealed = game.stage < 2
    zone3_sealed = game.stage < 3
    if zone2_sealed and zone3_sealed:
        return [(" LEVEL 2 — SEALED ", row, 0, GRID_W)]
    if zone3_sealed:
        return [(" LEVEL 3 — SEALED ", row, 0, boundary_x)]
    return []


def draw_base(stdscr, game, dim=False):
    """Renders the world. Used both for the normal overworld screen AND as
    the (dimmed) backdrop behind sign/terminal/congrats popups, so those
    never feel like a separate app — the world is still right there, just
    paused."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    base_dim = curses.A_DIM if dim else 0

    # A level is drawn once you've reached it, and stays drawn from then on
    # — clearing a level doesn't put it back under fog. Only what's still
    # ahead of you renders as a sealed block.
    ox, oy = 1, 3
    for y in range(GRID_H):
        for x in range(GRID_W):
            zone = _zone_for(x, y)
            if zone is not None and zone > game.stage:
                try:
                    stdscr.addstr(oy + y, ox + x * CELL_W, WALL_GLYPH * CELL_W,
                                  curses.A_DIM | base_dim)
                except curses.error:
                    pass
                continue
            glyph, kind = tile_glyph(game, x, y)
            cell = glyph * CELL_W if kind == "wall" else glyph + " "
            style = _TILE_STYLE_KEYS[kind]() | base_dim
            if kind == "computer":
                # the station's letter sits next to it, so the A-to-H order
                # is readable on the map; a solved one dims out
                sid = game.computers[(x, y)]
                cell = glyph + sid
                if game.stations[sid].solved:
                    style = CP_COMP | curses.A_DIM | base_dim
            try:
                stdscr.addstr(oy + y, ox + x * CELL_W, cell, style)
            except curses.error:
                pass

    for banner, row, x0, x1 in _zone_banners(game):
        center = (ox + x0 * CELL_W + ox + x1 * CELL_W) // 2
        banner_x0 = max(0, center - len(banner) // 2)
        try:
            stdscr.addstr(oy + row, banner_x0, banner,
                          curses.A_BOLD | curses.A_REVERSE | base_dim)
        except curses.error:
            pass

    blink = (game.tick_count // 2) % 2 == 0
    for e in (game.enemies if game.enemies_on else ()):
        zone = _zone_for(e["x"], e["y"])
        if zone is not None and zone > game.stage:
            continue
        try:
            stdscr.addstr(oy + e["y"], ox + e["x"] * CELL_W, "x" if blink else "+",
                          CP_ENEMY | curses.A_BOLD | base_dim)
        except curses.error:
            pass

    pstyle = CP_PLAYER | curses.A_BOLD
    if game.invuln > 0 and game.invuln % 2 == 0:
        pstyle |= curses.A_DIM
    try:
        # "[▶]": the arrow between brackets, so you never lose yourself among
        # the dots. Each tile is 2 columns wide; the "[" borrows the blank
        # column to the left and the "]" the blank column to the right.
        stdscr.addstr(oy + game.py, ox + game.px * CELL_W - 1,
                      "[" + FACING_GLYPH[game.facing] + "]", pstyle | base_dim)
    except curses.error:
        pass

    title_style = curses.A_BOLD | base_dim
    try:
        stdscr.addstr(0, 0, "TERMINALQUEST — SIGNAL LOST", title_style)
        stdscr.addstr(1, 0, "arrow keys move — walk into things"[: w - 1], curses.A_DIM | base_dim)
    except curses.error:
        pass

    hud_y = oy + GRID_H + 1
    hearts = "♥" * max(0, game.lives)
    try:
        nxt = game.next_station()
        hud = (f"{hearts}   *{game.score}   {game.solved_count()}/8 stations"
               + (f"   next: terminal {nxt}" if nxt else "   all stations done - head for X")
               + f"   [q] quit  [e] enemies {'on' if game.enemies_on else 'off'}"
               + f"  [b] sound {'on' if game.sound_on else 'off'}")
        stdscr.addstr(hud_y, 0, hud[: w - 1], base_dim)
        stdscr.addstr(hud_y + 1, 0, LEGEND[: w - 1], curses.A_DIM | base_dim)
    except curses.error:
        pass
    cur_stage = STAGES[game.stage - 1] if 1 <= game.stage <= len(STAGES) else None
    if cur_stage and cur_stage["gate_door"] and game.doors[cur_stage["gate_door"]]["locked"]:
        need = len(cur_stage["coins"])
        remaining = sum(1 for p in cur_stage["coins"] if game.coins.get(p, False))
        note = f"the way on is sealed — clear this level first ({need - remaining}/{need} coins found)"
        try:
            stdscr.addstr(hud_y + 2, 0, note[: w - 1], curses.A_DIM | base_dim)
        except curses.error:
            pass
    elif game.learned:
        learned_line = "so far: " + "  ·  ".join(game.learned)
        try:
            stdscr.addstr(hud_y + 2, 0, learned_line[: w - 1], CP_OK | base_dim)
        except curses.error:
            pass
    if game.message and not dim:
        style = curses.A_BOLD | (curses.A_REVERSE if game.flash > 0 else 0)
        try:
            stdscr.addstr(hud_y + 3, 0, game.message[: w - 1], style)
        except curses.error:
            pass
    return h, w


def draw_overworld(stdscr, game):
    draw_base(stdscr, game, dim=False)
    stdscr.refresh()


def draw_sign(stdscr, game):
    h, w = draw_base(stdscr, game, dim=True)
    lines = []
    for para in game.sign_text.split("\n"):
        lines.extend(textwrap.wrap(para, width=min(56, w - 10)) or [""])
    box_w = min(60, w - 4)
    top = max(1, h // 2 - len(lines) // 2 - 2)
    left = max(1, (w - box_w) // 2)
    try:
        stdscr.addstr(top - 1, left, "┌" + "─" * (box_w - 2) + "┐", curses.A_BOLD)
        for i, ln in enumerate(lines):
            stdscr.addstr(top + i, left, "│ " + ln.ljust(box_w - 4) + " │", curses.A_BOLD)
        stdscr.addstr(top + len(lines), left, "└" + "─" * (box_w - 2) + "┘", curses.A_BOLD)
        stdscr.addstr(top + len(lines) + 1, left, "(press any key)".center(box_w), curses.A_DIM)
    except curses.error:
        pass
    stdscr.refresh()


def draw_congrats(stdscr, game):
    """A full beat between levels — the next one is about to open, so this
    is the moment that actually says so. It waits for a deliberate SPACE
    rather than dismissing on any key: this screen used to vanish inside a
    frame because the player was still holding an arrow key from walking
    into the last coin, so movement keys are ignored here on purpose. The
    continue prompt is drawn as its own solid highlighted bar inside the
    box, not a dim line easy to miss underneath it."""
    h, w = draw_base(stdscr, game, dim=True)
    lines = game.congrats_text.split("\n") + ["", "PRESS SPACE TO CONTINUE"]
    box_w = min(60, w - 4)
    top = max(1, h // 2 - len(lines) // 2 - 2)
    left = max(1, (w - box_w) // 2)
    try:
        stdscr.addstr(top - 1, left, "┌" + "─" * (box_w - 2) + "┐", curses.A_BOLD)
        for i, ln in enumerate(lines):
            if ln == "PRESS SPACE TO CONTINUE":
                style = curses.A_BOLD | curses.A_REVERSE
            elif ln:
                style = CP_OK | curses.A_BOLD
            else:
                style = 0
            stdscr.addstr(top + i, left, "│ " + ln.center(box_w - 4) + " │", style)
        stdscr.addstr(top + len(lines), left, "└" + "─" * (box_w - 2) + "┘", curses.A_BOLD)
    except curses.error:
        pass
    stdscr.refresh()


def _popup(stdscr, game, rows, width=64):
    """A centered box over the dimmed map. `rows` is a list of (text, style);
    long text wraps. Used by the quiz screens."""
    h, w = draw_base(stdscr, game, dim=True)
    box_w = min(width, w - 4)
    lines = []
    for text, style in rows:
        for ln in (textwrap.wrap(text, width=box_w - 4) or [""]):
            lines.append((ln, style))
    top = max(1, h // 2 - len(lines) // 2 - 2)
    left = max(1, (w - box_w) // 2)
    try:
        stdscr.addstr(top - 1, left, "┌" + "─" * (box_w - 2) + "┐", curses.A_BOLD)
        for i, (ln, style) in enumerate(lines):
            stdscr.addstr(top + i, left, "│ " + ln.ljust(box_w - 4) + " │", style)
        stdscr.addstr(top + len(lines), left, "└" + "─" * (box_w - 2) + "┘", curses.A_BOLD)
    except curses.error:
        pass
    stdscr.refresh()


def draw_quitconfirm(stdscr, game):
    _popup(stdscr, game, [
        ("QUIT TERMINALQUEST?", CP_OK | curses.A_BOLD), ("", 0),
        ("Your progress this run isn't saved.", 0), ("", 0),
        ("Y = quit     N = keep playing", curses.A_BOLD | curses.A_REVERSE),
    ], width=44)


def draw_quizoffer(stdscr, game):
    _popup(stdscr, game, [
        (f"QUICK RECAP - LEVEL {game.quiz['level']}", CP_OK | curses.A_BOLD),
        ("", 0),
        (f"{QUIZ_LEN} short questions on what you just learned. Totally optional.", 0),
        ("", 0),
        ("Y = take the quiz     N = skip", curses.A_BOLD | curses.A_REVERSE),
    ])


def draw_quiz(stdscr, game):
    z = game.quiz
    q = game.quiz_question()
    rows = [(f"QUIZ  -  QUESTION {z['idx'] + 1} OF {len(z['items'])}", CP_OK | curses.A_BOLD), ("", 0),
            (q["prompt"], curses.A_BOLD), ("", 0)]
    if q["kind"] == "choice":
        for i, opt in enumerate(q["options"]):
            marker = "> " if (z["phase"] == "feedback" and i == q["correct"]) else "  "
            rows.append((f"{marker}{i + 1}) {opt}", 0))
        rows.append(("", 0))
    for ln in z["lines"]:
        bad = ln.startswith("bash said") or ln.startswith("Not quite")
        rows.append((ln, CP_ERR if bad else CP_OK))
    if z["phase"] == "feedback":
        last = z["idx"] + 1 >= len(z["items"])
        rows += [("", 0), ("PRESS SPACE TO " + ("FINISH" if last else "CONTINUE"), curses.A_BOLD | curses.A_REVERSE)]
    elif q["kind"] == "choice":
        rows += [("", 0), ("Press 1-%d to answer   Esc: skip quiz" % len(q["options"]), curses.A_DIM)]
    else:
        rows += [("", 0), (f"$ {game.input_buf}", curses.A_BOLD),
                 ("Type a command, Enter to run   Esc: skip quiz", curses.A_DIM)]
    _popup(stdscr, game, rows)


def draw_quizdone(stdscr, game):
    z = game.quiz
    _popup(stdscr, game, [
        ("QUIZ COMPLETE", CP_OK | curses.A_BOLD), ("", 0),
        (f"You got {z['right']} of {len(z['items'])} right.", 0), ("", 0),
        ("PRESS SPACE TO CONTINUE", curses.A_BOLD | curses.A_REVERSE),
    ])


def _first_word(line):
    return line.strip().split()[0] if line.strip() else ""


def _looks_like_an_error(text):
    return any(m in text for m in ("not found", "No such file", "cannot", "Usage:", "usage:"))


def _first_meaningful_line(text):
    for ln in text.split("\n"):
        if ln.strip():
            return ln
    return "(no output — that can be normal)"


def draw_terminal(stdscr, game):
    """The terminal panel: COMMAND / WHAT IT DOES / SYNTAX / YOUR TASK are
    always visible (not a one-time card), with the real prompt below them.
    Sized responsively — it grows with the terminal window instead of
    sitting at one small fixed size."""
    h, w = draw_base(stdscr, game, dim=True)
    st = game.stations[game.active_station]
    sid = game.active_station

    box_h = min(22, h - 6)
    box_w = min(100, w - 4)
    top = max(2, h - box_h - 3)
    left = max(1, (w - box_w) // 2)

    def put(y, x, text, style=0):
        try:
            stdscr.addstr(y, x, text[: box_w - 2], style)
        except curses.error:
            pass

    def full_bar(y, text, style):
        """A full-width action bar reads as THE thing to do next — a short
        colored label in an otherwise mostly-empty line is easy to miss."""
        try:
            stdscr.addstr(y, left + 1, text[: box_w - 2].ljust(box_w - 2), style)
        except curses.error:
            pass

    try:
        stdscr.addstr(top - 1, left, "┌" + "─" * (box_w - 2) + "┐", curses.A_BOLD)
        for i in range(box_h):
            stdscr.addstr(top + i, left, "│" + " " * (box_w - 2) + "│")
        stdscr.addstr(top + box_h, left, "└" + "─" * (box_w - 2) + "┘", curses.A_BOLD)
    except curses.error:
        pass

    put(top - 1, left + 2, f" {st.clue} ", curses.A_BOLD)
    progress = f"{game.solved_count()}/8 STATIONS"
    put(top - 1, left + box_w - len(progress) - 3, f" {progress} ", CP_OK | curses.A_BOLD | curses.A_REVERSE)

    if st.solved:
        countdown = "." * (1 + game.return_timer // 5)
        full_bar(top + 2, f" ✓ DOOR {sid}: SOLVED — {st.resolved_explain}",
                 CP_OK | curses.A_BOLD | curses.A_REVERSE)
        put(top + 4, left + 3, f"heading back to the ship{countdown}", curses.A_DIM)
        try:
            stdscr.addstr(top + box_h + 1, left, "(press anything to go now)"[: box_w], curses.A_DIM)
        except curses.error:
            pass
        stdscr.refresh()
        return

    # -- the always-visible reference panel ------------------------------
    inner_top = top + 1
    put(inner_top, left + 3, "COMMAND", CP_COMP | curses.A_BOLD)
    put(inner_top + 1, left + 5, st.hint, CP_OK | curses.A_BOLD)

    does_y = inner_top + 3
    put(does_y, left + 3, "WHAT IT DOES", CP_COMP | curses.A_BOLD)
    does_lines = textwrap.wrap(command_does(sid), width=max(20, box_w - 10)) or [""]
    for i, line in enumerate(does_lines):
        put(does_y + 1 + i, left + 5, line)

    syntax_y = does_y + 1 + len(does_lines) + 1
    put(syntax_y, left + 3, "SYNTAX", CP_COMP | curses.A_BOLD)
    put(syntax_y + 1, left + 5, COMMAND_SYNTAX[sid], curses.A_BOLD)

    task_y = syntax_y + 3
    put(task_y, left + 3, "YOUR TASK", CP_COMP | curses.A_BOLD)
    task_lines = textwrap.wrap(YOUR_TASK[sid], width=max(20, box_w - 10)) or [""]
    for i, line in enumerate(task_lines):
        put(task_y + 1 + i, left + 5, line)

    progress_y = task_y + 1 + len(task_lines) + 1
    dots = "●" * game.solved_count() + "○" * (8 - game.solved_count())
    put(progress_y, left + 3, f"{dots}   {game.solved_count()}/8 STATIONS CLEARED", curses.A_DIM | curses.A_BOLD)

    # -- last result + optional "why", anchored from the bottom ---------
    if st.transcript:
        _, last_out = st.transcript[-1]
        result_y = top + box_h - 7
        first_line = _first_meaningful_line(last_out)
        is_error = _looks_like_an_error(last_out)
        put(result_y, left + 3, f"$ {st.last_command}", curses.A_BOLD)
        put(result_y + 1, left + 5, first_line, CP_ERR if is_error else curses.A_DIM)
        if game.show_explanation:
            if is_error:
                meaning = "That's bash reporting the command didn't work — check SYNTAX above and the exact name."
            else:
                meaning = RESULT_MEANING.get(
                    _first_word(st.last_command),
                    "That's whatever bash actually printed — compare it with SYNTAX above.")
            put(result_y + 2, left + 5, f"Why: {meaning}")
        else:
            put(result_y + 2, left + 5, "Press ? to explain this result.", curses.A_DIM)
        if st.tip:
            put(result_y + 3, left + 5, st.tip, CP_DOOR | curses.A_BOLD)

    full_bar(top + box_h - 3, " ▶ TYPE THE COMMAND ABOVE, THEN PRESS ENTER", CP_OK | curses.A_BOLD | curses.A_REVERSE)
    put(top + box_h - 2, left + 3, f"$ {game.input_buf}", curses.A_BOLD)
    try:
        stdscr.addstr(top + box_h + 1, left,
                      "Tab: insert command   Enter: run   ?: explain last result   Esc: clear / leave"[: box_w],
                      curses.A_DIM)
    except curses.error:
        pass
    stdscr.refresh()


def draw_gameover(stdscr, game):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    msg = ["GAME OVER", "", "press any key to try again, or q to quit"]
    for i, ln in enumerate(msg):
        style = (CP_ERR | curses.A_BOLD) if i == 0 else 0
        try:
            stdscr.addstr(h // 2 - 1 + i, max(0, (w - len(ln)) // 2), ln, style)
        except curses.error:
            pass
    stdscr.refresh()


def draw_intro(stdscr, game):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    lines = [
        ("TERMINALQUEST", curses.A_BOLD | CP_OK),
        ("signal lost", curses.A_DIM),
        ("", 0),
        ("The ship's gone quiet. Eight stations, one signal.", 0),
        ("", 0),
        ("◀ ▲ ▼ ▶   arrow keys move", curses.A_BOLD),
        ("            you're the arrow — it points the way you're facing", curses.A_DIM),
        ("", 0),
        ("walk into things to interact with them:", 0),
        ("  x  " + " " * 1 + "a patrol — touching one costs a life", CP_ENEMY),
        ("  ▓  " + " " * 1 + "a locked door — find a way to open it", CP_DOOR),
        ("  ▣  " + " " * 1 + "an old terminal — try typing something", CP_COMP),
        ("  *  " + " " * 1 + "worth grabbing", CP_COIN),
        ("  X  " + " " * 1 + "the way out", CP_OK),
        ("", 0),
        ("press any key to start", curses.A_BOLD | curses.A_REVERSE),
    ]
    top = max(1, h // 2 - len(lines) // 2 - 2)
    for i, (text, style) in enumerate(lines):
        try:
            stdscr.addstr(top + i, max(2, (w - 60) // 2), text, style)
        except curses.error:
            pass
    stdscr.refresh()


# A tiny hand-built block font, 5 rows tall, just wide enough to spell the
# final win banner out of actual characters instead of a plain sentence —
# "make a design out of the characters." Only the letters the banner needs.
_BANNER_FONT = {
    "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "],
    "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
    "U": ["#   #", "#   #", "#   #", "#   #", " ### "],
    "W": ["#   #", "#   #", "# # #", "## ##", "#   #"],
    "I": ["#####", "  #  ", "  #  ", "  #  ", "#####"],
    "N": ["#   #", "##  #", "# # #", "#  ##", "#   #"],
    " ": ["  ", "  ", "  ", "  ", "  "],
}


def render_banner(word):
    """Render `word` as 5 lines of block-letter ASCII art, one string per
    row, letters separated by a single blank column. Falls back to a blank
    glyph for any character not in the tiny font above."""
    rows = ["" for _ in range(5)]
    for ch in word.upper():
        glyph = _BANNER_FONT.get(ch, _BANNER_FONT[" "])
        for r in range(5):
            rows[r] += glyph[r] + " "
    return [r.rstrip() for r in rows]


def draw_win(stdscr, game, typed="", error=None):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    banner_lines = render_banner("YOU WIN")
    lines = [
        "SIGNAL RESTORED",
        "",
        f"score: {game.score}     lives left: {game.lives}"
        + (f"     quiz: {game.quiz_right}/{game.quiz_taken}" if game.quiz_taken else ""),
        "",
        "the ship exhales. welcome back, operator.",
    ]
    if game.learned:
        lines += ["", "commands you actually used, for real: " + ", ".join(game.learned)]
    lines += [""]

    row = 1
    for ln in banner_lines:
        try:
            stdscr.addstr(row, max(0, (w - len(ln)) // 2), ln, CP_OK | curses.A_BOLD)
        except curses.error:
            pass
        row += 1
    row += 1
    for i, ln in enumerate(lines):
        style = (CP_OK | curses.A_BOLD) if i == 0 else 0
        try:
            stdscr.addstr(row + i, max(0, (w - len(ln)) // 2), ln, style)
        except curses.error:
            pass
    row += len(lines)
    # its own solid highlighted bar, not a dim afterthought line — this is
    # the one thing the player actually needs to know how to do here.
    prompt = " TYPE :wq THEN PRESS ENTER TO EXIT "
    try:
        stdscr.addstr(row, max(0, (w - len(prompt)) // 2), prompt,
                      curses.A_BOLD | curses.A_REVERSE)
    except curses.error:
        pass
    row += 2
    # echo what's actually been typed so far — without this, a player
    # mistyping ":wq" got no feedback at all, just a screen that silently
    # ate their keystrokes.
    typed_line = "> " + typed
    try:
        stdscr.addstr(row, max(0, (w - len(typed_line)) // 2), typed_line, curses.A_BOLD)
    except curses.error:
        pass
    if error:
        try:
            stdscr.addstr(row + 1, max(0, (w - len(error)) // 2), error, CP_ERR | curses.A_BOLD)
        except curses.error:
            pass
    stdscr.refresh()


MIN_H, MIN_W = 28, 112


def draw_too_small(stdscr):
    """Shown instead of the normal frame whenever the terminal is currently
    smaller than the game needs — checked every frame (not just at startup)
    so shrinking the window mid-game degrades to this message instead of
    crashing, and playing resumes automatically once it's grown back."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    msg = f"Terminal is {w}x{h}. Please resize to at least {MIN_W}x{MIN_H}."
    try:
        stdscr.addstr(0, 0, msg[: max(0, w - 1)])
    except curses.error:
        pass
    stdscr.refresh()


QUIT_COMBO = ":wq"


def feed_quit_combo(buf, ch, mode):
    """One step of the ":wq"-quits-from-anywhere easter egg, as a pure
    function so it's testable without a real curses loop. `buf` is the
    rolling buffer of the last few keys typed; returns (new_buf, matched).
    Resets (and never matches) while actually typing at a terminal prompt,
    where a colon/w/q are just ordinary command characters, and for
    non-printable/special keys (arrow keys and the like)."""
    if ch == -1:
        # curses' idle tick (no key pressed within the timeout) — not a
        # keystroke, so it must not wipe a combo the player is mid-way through.
        return buf, False
    if mode == "terminal" or not (0 <= ch < 256):
        return "", False
    buf = (buf + chr(ch))[-len(QUIT_COMBO):]
    return buf, buf == QUIT_COMBO


LAST_GAME = None  # the most recent Game main() ran, for the exit-time cheat sheet


CHEAT_SHEET = [
    ("ls", "ls", "list what's in this folder", "ls"),
    ("cat", "cat notes.txt", "print a file's contents", "cat"),
    ("cd", "cd projects", "move into a folder", "cd"),
    ("mkdir", "mkdir photos", "create a new folder", "mkdir"),
    ("touch", "touch todo.txt", "create a new, empty file", "touch"),
    ("cp", "cp a.txt b.txt", "copy a file (the original stays)", "cp"),
    ("mv", "mv old.txt new.txt", "move or rename a file", "mv"),
    ("rm", "rm junk.tmp", "delete a file (no undo!)", "rm"),
]


def cheat_sheet_text(learned=()):
    """The takeaway printed into the shell after the game closes. Commands
    you actually ran in this session are ticked."""
    lines = ["", "TerminalQuest cheat sheet — the 8 commands", "-" * 46]
    for word, example, meaning, key in CHEAT_SHEET:
        mark = "✓" if key in learned else " "
        lines.append(f" {mark} {example:<20} {meaning}")
    lines += ["-" * 46, "Try them for real in any terminal. Nice work, operator.", ""]
    return "\n".join(lines)


def wait_for_quit(stdscr, game=None):
    """Block on the win screen until the player types ":wq" and then Enter,
    like vim. Idle ticks (getch() == -1) between keystrokes are ignored.
    Wrong text followed by Enter no longer vanishes silently — it's echoed
    back with an error, and the line resets so they can try again."""
    buf, armed = "", False
    typed, error = "", None
    while True:
        if game is not None:
            draw_win(stdscr, game, typed=typed, error=error)
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (curses.KEY_ENTER, 10, 13):
            if armed:
                return
            error = f'not a command: "{typed}" — type :wq' if typed else "type :wq"
            buf, armed, typed = "", False, ""
            continue
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            typed = typed[:-1]
            error = None
        elif 0 <= ch < 256 and 32 <= ch <= 126:
            typed = (typed + chr(ch))[-40:]
            error = None
        buf, armed = feed_quit_combo(buf, ch, "win")


def handle_quiz_key(game, ch):
    """Route one keypress on any of the quiz screens (offer / question /
    summary). Pulled out of main() so it can be tested without curses."""
    if game.mode == "quizoffer":
        if ch in (ord("y"), ord("Y")):
            game.quiz_start()
        elif ch in (ord("n"), ord("N"), 27):
            game.quiz_skip()
    elif game.mode == "quiz":
        if ch == 27:
            game.quiz_skip()
        elif game.quiz["phase"] == "feedback":
            if ch == ord(" "):
                game.quiz_continue()
        elif game.quiz_question()["kind"] == "choice":
            if ord("1") <= ch <= ord("9"):
                game.quiz_submit(ch - ord("1"))
        elif ch in (curses.KEY_ENTER, 10, 13):
            game.quiz_submit()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            game.quiz_backspace()
        elif 32 <= ch <= 126:
            game.quiz_char(chr(ch))
    elif game.mode == "quizdone":
        if ch == ord(" "):
            game.quiz_finish()


def main(stdscr):
    curses.curs_set(0)
    try:
        curses.start_color()
        curses.use_default_colors()
        setup_colors()
    except curses.error:
        pass

    root = os.path.expanduser(os.environ.get("TERMINALQUEST_ROOT", DEFAULT_ROOT))
    reset = "--reset" in sys.argv
    global LAST_GAME
    game = Game(root, reset=reset, enemies="--no-enemies" not in sys.argv)
    LAST_GAME = game
    stdscr.timeout(100)  # ms per tick

    draw_intro(stdscr, game)
    # getch() returns -1 every 100ms with no key (see stdscr.timeout above),
    # so wait for a REAL keypress — otherwise the intro flashes for a tenth
    # of a second and the player never gets to read the controls.
    while stdscr.getch() == -1:
        pass

    while True:
        h, w = stdscr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            draw_too_small(stdscr)
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q")):
                return
            continue

        game.tick()
        if game.mode == "terminal":
            game.tick_terminal()
        if game.bell:
            game.bell = False
            if game.sound_on:
                try:
                    curses.beep()
                except curses.error:
                    pass

        if game.mode == "overworld":
            draw_overworld(stdscr, game)
        elif game.mode == "sign":
            draw_sign(stdscr, game)
        elif game.mode == "congrats":
            draw_congrats(stdscr, game)
        elif game.mode == "terminal":
            draw_terminal(stdscr, game)
        elif game.mode == "quitconfirm":
            draw_quitconfirm(stdscr, game)
        elif game.mode == "quizoffer":
            draw_quizoffer(stdscr, game)
        elif game.mode == "quiz":
            draw_quiz(stdscr, game)
        elif game.mode == "quizdone":
            draw_quizdone(stdscr, game)
        elif game.mode == "gameover":
            draw_gameover(stdscr, game)
        elif game.mode == "win":
            # ":wq" then Enter — and only that — closes the game out here,
            # once it's actually over. Not "any key": a held-over arrow key
            # from walking onto the exit tile would otherwise close this
            # instantly, before the banner is even seen. wait_for_quit does
            # its own drawing so it can echo what's typed and flag mistakes.
            wait_for_quit(stdscr, game)
            return

        ch = stdscr.getch()
        if ch == -1:
            continue

        if game.mode == "overworld":
            if ch in (ord("q"), ord("Q")):
                game.ask_quit()
            elif ch in (ord("e"), ord("E")):
                game.toggle_enemies()
            elif ch in (ord("b"), ord("B")):
                game.toggle_sound()
            elif ch in (ord("t"), ord("T")):
                game.high_contrast = not game.high_contrast
                setup_colors(game.high_contrast)
                game.message = "HIGH CONTRAST ON" if game.high_contrast else "COLOR ACCENTS ON"
            elif ch == curses.KEY_UP:
                game.try_move(0, -1)
            elif ch == curses.KEY_DOWN:
                game.try_move(0, 1)
            elif ch == curses.KEY_LEFT:
                game.try_move(-1, 0)
            elif ch == curses.KEY_RIGHT:
                game.try_move(1, 0)

        elif game.mode == "quitconfirm":
            if ch in (ord("y"), ord("Y")):
                return
            elif ch in (ord("n"), ord("N"), 27):
                game.cancel_quit()

        elif game.mode == "sign":
            game.dismiss_sign()

        elif game.mode == "congrats":
            # deliberately not "any key" — see draw_congrats for why.
            if ch == ord(" "):
                game.dismiss_congrats()

        elif game.mode == "terminal":
            if game.stations[game.active_station].solved:
                game.exit_terminal()   # solved — any key at all takes you back to the map
            elif ch == 27:
                game.terminal_escape()
            elif ch in (curses.KEY_ENTER, 10, 13):
                game.terminal_submit()
            elif ch == 9:  # Tab: load the shown command into the prompt, don't run it
                game.terminal_suggest()
            elif ch == ord("?") and not game.input_buf:
                game.toggle_explanation()
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                game.terminal_backspace()
            elif 32 <= ch <= 126:
                game.terminal_char(chr(ch))

        elif game.mode in ("quizoffer", "quiz", "quizdone"):
            handle_quiz_key(game, ch)

        elif game.mode == "gameover":
            if ch in (ord("q"), ord("Q")):
                return
            else:
                game.retry()


# ==========================================================================
# Headless self-test — drives the same Game class the UI uses, no curses.
# Run with: python3 terminalquest.py --selftest
# ==========================================================================

def selftest():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="tq_arcade_selftest_")
    g = Game(tmp, reset=True, quiz=False)

    assert (g.px, g.py) == PLAYER_START

    # door1 (doorAB) is locked before station A is solved
    door_pos = DOORS["doorAB"]["pos"]
    approach = (door_pos[0] - 1, door_pos[1])
    walk_to(g, approach)
    g.try_move(1, 0)
    assert (g.px, g.py) == approach, "should have been blocked by the locked door"
    assert g.message in LOCKED_LINES, g.message

    solve_commands = {
        "A": "ls", "B": "cat key.txt", "C": "cd vault", "D": "mkdir stash",
        "E": "touch spare.key", "F": "cp template.txt backup.txt",
        "G": "mv draft.txt final.txt", "H": "rm jam.lock",
    }

    for sid in STATION_ORDER:
        comp_pos = next(pos for pos, s in COMPUTERS.items() if s == sid)
        walk_to(g, comp_pos)
        assert g.mode == "terminal" and g.active_station == sid, (sid, g.mode, g.active_station)

        # a wrong command first — should just be a real bash error, never solve it
        g.input_buf = "frobnicate"
        g.terminal_submit()
        assert not g.stations[sid].solved

        # '?' reveals an explanation of that (wrong) result, only on request
        assert g.show_explanation is False
        g.toggle_explanation()
        assert g.show_explanation is True
        g.toggle_explanation()
        assert g.show_explanation is False

        # Tab loads the exact solving command without submitting it
        g.terminal_suggest()
        assert g.input_buf == g.stations[sid].hint
        assert not g.stations[sid].solved

        g.input_buf = solve_commands[sid]
        g.terminal_submit()
        assert g.stations[sid].solved, (sid, g.stations[sid].transcript)
        door_name = STATION_TO_DOOR[sid]
        assert g.doors[door_name]["locked"] is False

        if sid in GATE_STATIONS:
            # solving the last station of a level jumps straight to the
            # congrats beat instead of the normal "solved, press any key"
            # terminal state — confirm that actually fired.
            assert g.mode == "congrats", (sid, g.mode)
        g.exit_terminal()
        assert g.mode == "overworld"

        # re-entering an already-solved station goes straight back to "solved"
        g.enter_terminal(sid)
        assert g.mode == "terminal" and g.stations[sid].solved
        g.exit_terminal()

        if sid == "C":
            # level 1's gate needs every one of its coins too, not just
            # station D solved — collect them all before D is reached.
            assert g.stage == 1
            for coin_pos in BAND0_COINS:
                if g.coins[coin_pos]:
                    walk_to(g, coin_pos)
            assert all(not g.coins[p] for p in BAND0_COINS)

        if sid == "E":
            # same idea, one level in: level 2's gate needs its own coins
            # collected before F (its gate station) is reached.
            assert g.stage == 2
            for coin_pos in BAND1_COINS:
                if g.coins[coin_pos]:
                    walk_to(g, coin_pos)
            assert all(not g.coins[p] for p in BAND1_COINS)

    # B's secret closet should be open by now
    assert g.grid[HIDDEN_CLOSET_CELLS[0][1]][HIDDEN_CLOSET_CELLS[0][0]] == "."

    # every station solved — but the exit stays shut until all 8 coins are in
    walk_to(g, EXIT_POS)
    assert g.mode == "sign" and "SIGNAL INCOMPLETE" in g.sign_text, g.mode
    assert (g.px, g.py) != EXIT_POS
    g.dismiss_sign()
    collect_all_coins(g)
    assert g.coins_remaining() == 0
    walk_to(g, EXIT_POS)
    assert g.mode == "win", g.mode
    assert set(g.learned) == {"ls", "cat", "cd", "mkdir", "touch", "cp", "mv", "rm"}

    # --- enemy collision test, isolated ---
    g2 = Game(tmp + "_e", reset=True)
    e = g2.enemies[0]
    g2.px, g2.py = e["x"] + 1, e["y"]
    start_lives = g2.lives
    for _ in range(e["period"] + 1):
        g2.tick()
    assert g2.lives == start_lives - 1, "enemy should have hit the player"
    assert (g2.px, g2.py) == PLAYER_START, "player should respawn at start"

    # --- facing indicator + a sign + a coin, isolated, found via the data
    # structures themselves rather than hardcoded coordinates ---
    g3 = Game(tmp + "_f", reset=True)
    assert g3.facing == "right"
    g3.try_move(0, 1)
    assert g3.facing == "down"
    g3.try_move(0, -1)
    assert g3.facing == "up"
    sign_pos = next(iter(SIGNS))
    walk_to(g3, sign_pos)
    assert g3.mode == "sign"
    g3.dismiss_sign()
    coin_pos = next(iter(COINS_INIT))
    if coin_pos != sign_pos:
        walk_to(g3, coin_pos)
        assert g3.coins[coin_pos] is False and g3.score == 1

    # --- optional quiz, headless: take one and get everything right ---
    g5 = Game(tmp + "_q", reset=True, rng=random.Random(1))
    g5.begin_quiz_offer(1, "overworld")
    g5.quiz_start()
    for _ in range(QUIZ_LEN):
        q = g5.quiz_question()
        if q["kind"] == "type":
            g5.input_buf = q["answer"]
            g5.quiz_submit()
        else:
            g5.quiz_submit(q["correct"])
        g5.quiz_continue()
    assert g5.mode == "quizdone" and g5.quiz["right"] == QUIZ_LEN, g5.mode
    g5.quiz_finish()
    assert g5.mode == "overworld"

    print("SELFTEST PASSED")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(tmp + "_e", ignore_errors=True)
    shutil.rmtree(tmp + "_f", ignore_errors=True)


def run():
    """Start the game and handle the ways it can end. Returns the exit code."""
    # curses waits ESCDELAY ms after an Esc to see if it starts an arrow-key
    # sequence — the default is a full second, which makes Esc feel broken.
    os.environ.setdefault("ESCDELAY", "25")
    code = 0
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        code = 130            # Ctrl+C is a perfectly normal way to leave
    except curses.error as e:
        print("TerminalQuest needs a real terminal window with curses support, at least "
              f"{MIN_W}x{MIN_H}.\nRun it directly in Terminal (not piped or inside an editor).\n"
              f"(details: {e})", file=sys.stderr)
        return 1
    print(cheat_sheet_text(LAST_GAME.learned if LAST_GAME else ()))
    return code


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        sys.exit(run())
