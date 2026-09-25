"""
Test suite for TerminalQuest.

Run with:
    pip install -r requirements-dev.txt
    pytest

Two layers are covered on purpose:

1. `run_command` — the real-bash execution layer. These tests actually shell
   out to /bin/bash, jailed to a tmp sandbox, exactly like the game does.
2. `Game` — the pure game-logic class. It has zero curses dependency, so
   every rule (locked doors, enemy collisions, station puzzles, the lesson
   flow) is testable without a real terminal.

Notes for contributors touching the map (terminalquest.build_grid /
DOORS / COMPUTERS / etc.): these tests navigate with `terminalquest.walk_to`,
a small BFS helper, instead of hardcoded step counts. If you move a room or
a door, these tests should keep passing without being touched — that's the
point. If you add a new station, please add a test alongside it.
"""
import curses
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import terminalquest as tq


# ==========================================================================
# fixtures
# ==========================================================================

@pytest.fixture
def sandbox(tmp_path):
    """A throwaway sandbox root, cleaned up automatically by pytest."""
    root = tmp_path / "tq_sandbox"
    root.mkdir()
    return str(root)


@pytest.fixture
def game(sandbox):
    """A freshly built Game, same as a new playthrough."""
    return tq.Game(sandbox, reset=True)


SOLVE_COMMANDS = {
    "A": "ls", "B": "cat key.txt", "C": "cd vault", "D": "mkdir stash",
    "E": "touch spare.key", "F": "cp template.txt backup.txt",
    "G": "mv draft.txt final.txt", "H": "rm jam.lock",
}


def enter(game, station_id):
    """Walk to a station's computer and enter its terminal. Solves every
    earlier station on the route first, since later doors are locked until
    then — this is the same order a real playthrough is forced into."""
    for prior in tq.STATION_ORDER:
        if prior == station_id:
            break
        if not game.stations[prior].solved:
            solve(game, prior)
    if tq.STATION_ORDER.index(station_id) >= tq.STATION_ORDER.index("E"):
        # the shaft to the bottom band needs every level-1 coin, not just D
        # solved — collecting the last one here (D already solved) can flip
        # the game straight to the congrats beat, so clear that before
        # trying to walk anywhere else.
        for coin_pos in tq.BAND0_COINS:
            if game.coins[coin_pos]:
                tq.walk_to(game, coin_pos)
        game.dismiss_congrats()
    if tq.STATION_ORDER.index(station_id) >= tq.STATION_ORDER.index("G"):
        # doorFG needs every level-2 coin too, not just F solved — same
        # congrats caveat as above.
        for coin_pos in tq.BAND1_COINS:
            if game.coins[coin_pos]:
                tq.walk_to(game, coin_pos)
        game.dismiss_congrats()
    pos = next(pos for pos, sid in tq.COMPUTERS.items() if sid == station_id)
    tq.walk_to(game, pos)
    assert game.mode == "terminal" and game.active_station == station_id


def solve(game, station_id):
    enter(game, station_id)
    game.input_buf = SOLVE_COMMANDS[station_id]
    game.terminal_submit()
    assert game.stations[station_id].solved, game.stations[station_id].transcript
    game.exit_terminal()


# ==========================================================================
# run_command — the real-bash layer
# ==========================================================================

class TestRunCommand:
    def test_basic_command_runs_for_real(self, sandbox):
        with open(os.path.join(sandbox, "hello.txt"), "w") as f:
            f.write("hi\n")
        cwd, out, err = tq.run_command("ls", sandbox, sandbox)
        assert "hello.txt" in out
        assert err == ""

    def test_cwd_tracks_across_calls(self, sandbox):
        os.makedirs(os.path.join(sandbox, "sub"))
        cwd, out, err = tq.run_command("cd sub", sandbox, sandbox)
        assert cwd == os.path.realpath(os.path.join(sandbox, "sub"))

    def test_unknown_command_is_a_real_bash_error(self, sandbox):
        cwd, out, err = tq.run_command("frobnicate", sandbox, sandbox)
        assert "not found" in err or "not found" in out

    def test_empty_line_is_a_noop(self, sandbox):
        cwd, out, err = tq.run_command("   ", sandbox, sandbox)
        assert (cwd, out, err) == (sandbox, "", "")

    @pytest.mark.parametrize("dangerous", [
        "sudo rm -rf /",
        "rm -rf /",
        "rm -fr /",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda1",
        ":(){ :|:& };:",
    ])
    def test_dangerous_patterns_are_blocked(self, sandbox, dangerous):
        cwd, out, err = tq.run_command(dangerous, sandbox, sandbox)
        assert cwd == sandbox
        assert out == ""
        assert err != ""

    def test_cannot_cd_outside_the_sandbox(self, sandbox):
        cwd, out, err = tq.run_command("cd " + "../" * 20, sandbox, sandbox)
        real_root = os.path.realpath(sandbox)
        real_cwd = os.path.realpath(cwd)
        assert real_cwd == real_root or real_cwd.startswith(real_root + os.sep)

    def test_rm_inside_sandbox_is_allowed(self, sandbox):
        target = os.path.join(sandbox, "junk.txt")
        with open(target, "w") as f:
            f.write("x")
        tq.run_command("rm junk.txt", sandbox, sandbox)
        assert not os.path.exists(target)


# ==========================================================================
# the map itself — sanity checks independent of any single station
# ==========================================================================

class TestMap:
    def test_every_station_has_a_computer_and_a_door(self, game):
        assert set(game.computers.values()) == set(tq.STATION_ORDER)
        assert set(d["station"] for d in tq.DOORS.values()) == set(tq.STATION_ORDER)

    def test_no_two_doors_share_a_row_and_are_adjacent_in_x(self):
        # regression guard for "all the doors are in one line": at least two
        # distinct rows must be in use across the door layout.
        rows = {pos[1] for pos in (d["pos"] for d in tq.DOORS.values())}
        assert len(rows) >= 2

    def test_player_start_is_on_open_floor(self, game):
        x, y = tq.PLAYER_START
        assert not game.wall(x, y)

    def test_exit_is_unreachable_until_the_final_gate_opens(self, game):
        assert tq.bfs_route(game, tq.EXIT_POS) is None

    def test_enemies_are_not_all_on_the_same_row(self, game):
        # regression guard: every enemy used to patrol row 0 of its room,
        # which reads as "all the enemies are in one line" just like the
        # doors did. At least two distinct absolute rows must be in use.
        rows = {e["y"] for e in game.enemies}
        assert len(rows) >= 2

    def test_no_enemy_patrols_over_a_door_computer_or_coin(self, game):
        for e in game.enemies:
            for name, d in game.doors.items():
                if d["pos"][1] == e["y"]:
                    assert not (e["min"] <= d["pos"][0] <= e["max"]), \
                        f"enemy at row {e['y']} can walk onto door {name}"
            for pos in game.computers:
                if pos[1] == e["y"]:
                    assert not (e["min"] <= pos[0] <= e["max"]), \
                        f"enemy at row {e['y']} can walk onto computer at {pos}"
            for pos in game.coins:
                if pos[1] == e["y"]:
                    assert not (e["min"] <= pos[0] <= e["max"]), \
                        f"enemy at row {e['y']} can walk onto coin at {pos}"


class TestTileGlyph:
    """tile_glyph() is the pure, curses-free function the renderer uses to
    decide what belongs at a tile. A wall always wins over whatever object
    is nominally placed there — this is what keeps the secret closet coin
    (and anything else sealed behind a wall) from showing through it."""

    def test_sealed_closet_shows_as_a_wall_not_its_coin(self, game):
        cell = tq.HIDDEN_CLOSET_CELLS[0]
        assert game.wall(*cell)
        glyph, kind = tq.tile_glyph(game, *cell)
        assert kind == "wall"
        assert glyph == tq.WALL_GLYPH

    def test_closet_coin_appears_once_the_wall_opens(self, game):
        cell = tq.HIDDEN_CLOSET_CELLS[0]
        solve(game, "B")  # B's effect reveals the closet
        assert not game.wall(*cell)
        glyph, kind = tq.tile_glyph(game, *cell)
        assert kind == "coin"
        assert glyph == "*"

    def test_computer_tile_reports_as_computer(self, game):
        pos = next(iter(game.computers))
        glyph, kind = tq.tile_glyph(game, *pos)
        assert kind == "computer" and glyph == "▣"

    def test_locked_and_unlocked_door_glyphs_differ(self, game):
        pos = tq.DOORS["doorAB"]["pos"]
        glyph, kind = tq.tile_glyph(game, *pos)
        assert kind == "door_locked" and glyph == "▓"
        solve(game, "A")
        glyph, kind = tq.tile_glyph(game, *pos)
        assert kind == "door_unlocked" and glyph == "'"

    def test_ordinary_floor_tile(self, game):
        # any open tile with nothing on it
        x, y = tq.PLAYER_START[0] + 1, tq.PLAYER_START[1] + 1
        assert not game.wall(x, y)
        glyph, kind = tq.tile_glyph(game, x, y)
        assert kind == "floor" and glyph == "."


# ==========================================================================
# movement, walls, doors
# ==========================================================================

class TestMovement:
    def test_starts_at_player_start(self, game):
        assert (game.px, game.py) == tq.PLAYER_START

    def test_walking_into_a_wall_does_not_move(self, game):
        game.px, game.py = 0, 0  # corner, walls on two sides
        game.try_move(-1, 0)
        assert (game.px, game.py) == (0, 0)

    def test_facing_updates_on_move(self, game):
        assert game.facing == "right"
        game.try_move(0, 1)
        assert game.facing == "down"
        game.try_move(0, -1)
        assert game.facing == "up"
        game.try_move(-1, 0)
        assert game.facing == "left"

    def test_locked_door_blocks_entry(self, game):
        gx, gy = tq.DOORS["doorAB"]["pos"]
        tq.walk_to(game, (gx - 1, gy))
        game.try_move(1, 0)
        assert (game.px, game.py) == (gx - 1, gy)
        assert game.message in tq.LOCKED_LINES

    def test_door_opens_once_its_station_is_solved(self, game):
        solve(game, "A")
        gx, gy = tq.DOORS["doorAB"]["pos"]
        tq.walk_to(game, (gx - 1, gy))
        game.try_move(1, 0)
        assert (game.px, game.py) == (gx, gy)


class TestCoinsAndSigns:
    def test_walking_onto_a_coin_collects_it_once(self, game):
        pos = next(iter(tq.COINS_INIT))
        tq.walk_to(game, pos)
        assert game.coins[pos] is False
        assert game.score == 1

    def test_walking_into_a_sign_opens_it_and_pauses_movement(self, game):
        pos = next(iter(tq.SIGNS))
        tq.walk_to(game, pos)
        assert game.mode == "sign"
        game.dismiss_sign()
        assert game.mode == "overworld"


class TestEnemies:
    def test_enemy_collision_costs_a_life_and_respawns_player(self, game):
        e = game.enemies[0]
        game.px, game.py = e["x"] + 1, e["y"]
        start_lives = game.lives
        for _ in range(e["period"] + 1):
            game.tick()
        assert game.lives == start_lives - 1
        assert (game.px, game.py) == tq.PLAYER_START

    def test_invulnerability_window_prevents_double_hit(self, game):
        e = game.enemies[0]
        game.px, game.py = e["x"] + 1, e["y"]
        for _ in range(e["period"] + 1):
            game.tick()
        lives_after_first_hit = game.lives
        game.invuln = 5
        game._hit()
        assert game.lives == lives_after_first_hit

    def test_three_hits_ends_the_game(self, game):
        game.lives = 1
        game.invuln = 0
        game._hit()
        assert game.mode == "gameover"


# ==========================================================================
# stations — each solved by exactly one real command
# ==========================================================================

class TestStations:
    @pytest.mark.parametrize("station_id,command,expected_word", [
        ("A", "ls", "ls"),
        ("B", "cat key.txt", "cat"),
        ("C", "cd vault", "cd"),
        ("D", "mkdir stash", "mkdir"),
        ("E", "touch spare.key", "touch"),
        ("F", "cp template.txt backup.txt", "cp"),
        ("G", "mv draft.txt final.txt", "mv"),
        ("H", "rm jam.lock", "rm"),
    ])
    def test_station_solved_by_its_one_command(self, game, station_id, command, expected_word):
        enter(game, station_id)
        game.input_buf = command
        game.terminal_submit()
        assert game.stations[station_id].solved
        assert expected_word in game.learned
        door_name = tq.STATION_TO_DOOR[station_id]
        if station_id in tq.GATE_STATIONS:
            # D and F are level gates: each also needs every coin for its
            # level, covered by TestStageGates below. Solving the station
            # alone should NOT open the gate.
            assert game.doors[door_name]["locked"] is True
        else:
            assert game.doors[door_name]["locked"] is False

    def test_wrong_command_does_not_solve_a_station(self, game):
        enter(game, "A")
        game.input_buf = "frobnicate"
        game.terminal_submit()
        assert not game.stations["A"].solved

    def test_solving_b_reveals_the_hidden_closet(self, game):
        cell = tq.HIDDEN_CLOSET_CELLS[0]
        assert game.wall(*cell)
        solve(game, "B")
        assert not game.wall(*cell)
        assert game.hidden_revealed is True

    def test_wrong_command_never_solves_a_pure_absence_station(self, game):
        # station H (rm) is solved by a file's ABSENCE, which made an
        # earlier build_stations bug specifically invisible for it: a
        # never-set-up folder also has no jam.lock, so a naive "does this
        # already look solved" check reads a blank folder as already done.
        enter(game, "H")
        assert os.path.exists(os.path.join(game.stations["H"].root, "jam.lock"))
        game.input_buf = "frobnicate"
        game.terminal_submit()
        assert not game.stations["H"].solved

    def test_a_plain_relaunch_starts_the_stations_fresh(self, sandbox):
        # Progress isn't saved, so training files must not persist either:
        # a solved G (draft.txt -> final.txt) has to be undone on relaunch,
        # or the leftover final.txt would mark G solved behind the game's back.
        g1 = tq.Game(sandbox, reset=False)
        solve(g1, "G")
        g_root = g1.stations["G"].root
        assert os.path.isfile(os.path.join(g_root, "final.txt"))

        g2 = tq.Game(sandbox, reset=False)  # plain relaunch, no --reset
        assert os.path.isfile(os.path.join(g_root, "draft.txt"))
        assert not os.path.exists(os.path.join(g_root, "final.txt"))
        assert not g2.stations["G"].solved

    def test_an_unrelated_command_cannot_solve_a_station_after_relaunch(self, sandbox):
        g1 = tq.Game(sandbox, reset=False)
        solve(g1, "H")  # rm jam.lock
        g2 = tq.Game(sandbox, reset=False)
        enter(g2, "H")
        g2.input_buf = "pwd"
        g2.terminal_submit()
        assert not g2.stations["H"].solved

    def test_a_deleted_setup_file_is_restored_on_relaunch(self, sandbox):
        g1 = tq.Game(sandbox, reset=False)
        vault = os.path.join(g1.stations["C"].root, "vault")
        shutil.rmtree(vault)
        tq.Game(sandbox, reset=False)
        assert os.path.isdir(vault)

    @pytest.mark.parametrize("station_id,wrong", [
        ("D", "mkdir mystuff"),
        ("D", "mkdir anything"),
        ("E", "touch random.txt"),
    ])
    def test_wrong_target_does_not_solve_but_gets_a_nudge(self, game, station_id, wrong):
        enter(game, station_id)
        game.input_buf = wrong
        game.terminal_submit()
        st = game.stations[station_id]
        assert not st.solved
        assert "this mission needs" in st.transcript[-1][1]

    @pytest.mark.parametrize("station_id,command", [
        ("D", "mkdir -p stash"),
        ("D", "mkdir ./stash"),
        ("E", "printf '' > spare.key"),
        ("E", "touch ./spare.key"),
    ])
    def test_any_command_that_produces_the_right_result_solves_it(self, game, station_id, command):
        enter(game, station_id)
        game.input_buf = command
        game.terminal_submit()
        assert game.stations[station_id].solved

    def test_a_file_named_stash_is_not_a_folder_named_stash(self, game):
        enter(game, "D")
        game.input_buf = "touch stash"
        game.terminal_submit()
        assert not game.stations["D"].solved

    def test_solved_count_tracks_all_eight_stations(self, game):
        assert game.solved_count() == 0
        solve(game, "A")
        assert game.solved_count() == 1


# ==========================================================================
# the lesson (teach-first) flow
# ==========================================================================

class TestHatchGate:
    """The shaft to the bottom band ('hatch') is the one door with an extra
    condition: station D solved AND every band-0 coin collected. Until then
    the bottom band is fully fogged out, not merely locked."""

    def test_hatch_stays_locked_with_d_solved_but_coins_missing(self, game):
        enter(game, "D")
        game.input_buf = "mkdir stash"
        game.terminal_submit()
        assert game.stations["D"].solved
        assert game.doors["hatch"]["locked"] is True

    def test_hatch_stays_locked_with_coins_collected_but_d_unsolved(self, game):
        for sid in ("A", "B", "C"):
            solve(game, sid)  # opens the doors into each coin's room
        for coin_pos in tq.BAND0_COINS:
            tq.walk_to(game, coin_pos)
        assert all(not game.coins[p] for p in tq.BAND0_COINS)
        assert game.doors["hatch"]["locked"] is True

    def test_hatch_opens_once_both_conditions_are_met(self, game):
        enter(game, "D")
        game.input_buf = "mkdir stash"
        game.terminal_submit()
        game.exit_terminal()
        for coin_pos in tq.BAND0_COINS:
            if game.coins[coin_pos]:
                tq.walk_to(game, coin_pos)
        assert game.doors["hatch"]["locked"] is False

    def test_last_coin_collected_after_d_solved_still_opens_it(self, game):
        for sid in ("A", "B", "C"):
            solve(game, sid)
        for coin_pos in tq.BAND0_COINS[:-1]:
            tq.walk_to(game, coin_pos)
        enter(game, "D")
        game.input_buf = "mkdir stash"
        game.terminal_submit()
        game.exit_terminal()
        assert game.doors["hatch"]["locked"] is True  # one coin still out there
        tq.walk_to(game, tq.BAND0_COINS[-1])
        assert game.doors["hatch"]["locked"] is False


class TestLessonPanel:
    """The COMMAND/WHAT IT DOES/SYNTAX/YOUR TASK panel is always visible at
    a station's terminal — there's no one-time card and no gate to dismiss.
    Tab loads the shown command without solving the station; '?' toggles an
    explanation of the last result, only once something has actually run."""

    def test_panel_content_is_available_immediately_on_first_visit(self, game):
        pos = next(pos for pos, sid in tq.COMPUTERS.items() if sid == "A")
        tq.walk_to(game, pos)
        assert game.mode == "terminal" and game.active_station == "A"
        assert tq.COMMAND_SYNTAX["A"] == "ls"
        assert tq.YOUR_TASK["A"]
        assert tq.command_does("A")

    def test_tab_loads_the_command_without_solving(self, game):
        enter(game, "A")
        assert game.input_buf == ""
        game.terminal_suggest()
        assert game.input_buf == game.stations["A"].hint == "ls"
        assert not game.stations["A"].solved

    def test_tab_does_nothing_once_solved(self, game):
        solve(game, "A")
        game.enter_terminal("A")
        game.input_buf = ""
        game.terminal_suggest()
        assert game.input_buf == ""

    def test_explanation_toggle_requires_a_prior_attempt(self, game):
        enter(game, "A")
        assert game.show_explanation is False
        game.toggle_explanation()  # nothing run yet — no-op
        assert game.show_explanation is False

        game.input_buf = "ls"
        game.terminal_submit()
        game.toggle_explanation()
        assert game.show_explanation is True

    def test_new_submission_resets_the_explanation_toggle(self, game):
        enter(game, "A")
        game.input_buf = "ls"
        game.terminal_submit()
        game.toggle_explanation()
        assert game.show_explanation is True
        game.input_buf = "ls"
        game.terminal_submit()
        assert game.show_explanation is False

    def test_every_station_has_distinct_lesson_text(self, game):
        lessons = {sid: st.lesson for sid, st in game.stations.items()}
        assert len(set(lessons.values())) == len(lessons)

    def test_every_station_has_syntax_and_task_text(self, game):
        for sid in tq.STATION_ORDER:
            assert tq.COMMAND_SYNTAX[sid]
            assert tq.YOUR_TASK[sid]
            assert tq.command_does(sid)

    def test_a_failed_command_is_recognized_as_an_error_not_explained_as_success(self, game):
        # a real bug: the raw output of a failed `cat` starts with a blank
        # line before bash's error text, so naively taking the first line
        # picked up "" instead of the actual error — and the '?' explanation
        # confidently described what a SUCCESSFUL cat does, which is
        # actively misleading right after a command just failed.
        enter(game, "B")
        game.input_buf = "cat nosuchfile.txt"
        game.terminal_submit()
        _, last_out = game.stations["B"].transcript[-1]
        assert tq._looks_like_an_error(last_out)
        assert tq._first_meaningful_line(last_out).strip() != ""
        assert "No such file" in tq._first_meaningful_line(last_out)


# ==========================================================================
# full playthrough — all eight stations, then out the gate
# ==========================================================================

class TestFullPlaythrough:
    def test_walking_the_whole_map_wins(self, game):
        for sid in tq.STATION_ORDER:
            solve(game, sid)

        tq.collect_all_coins(game)
        tq.walk_to(game, tq.EXIT_POS)
        assert game.mode == "win"
        assert set(game.learned) == {"ls", "cat", "cd", "mkdir", "touch", "cp", "mv", "rm"}

    def test_exit_refuses_a_win_with_coins_missing(self, game):
        for sid in tq.STATION_ORDER:
            solve(game, sid)
        left = game.coins_remaining()
        assert left > 0  # solving stations alone doesn't collect the optional coins
        tq.walk_to(game, tq.EXIT_POS)
        assert game.mode == "sign"
        assert "SIGNAL INCOMPLETE" in game.sign_text
        assert f"{left} of {len(tq.COINS_INIT)}" in game.sign_text
        assert (game.px, game.py) != tq.EXIT_POS

    def test_the_incomplete_signal_message_is_actually_drawn(self, game):
        for sid in tq.STATION_ORDER:
            solve(game, sid)
        tq.walk_to(game, tq.EXIT_POS)
        screen = FakeScreen()
        tq.draw_sign(screen, game)
        dump = screen.dump()
        assert "SIGNAL INCOMPLETE" in dump
        assert "still uncollected" in dump

    def test_exit_opens_once_the_last_missing_coin_is_collected(self, game):
        for sid in tq.STATION_ORDER:
            solve(game, sid)
        tq.walk_to(game, tq.EXIT_POS)
        assert game.mode == "sign"
        game.dismiss_sign()
        tq.collect_all_coins(game)
        assert game.coins_remaining() == 0
        tq.walk_to(game, tq.EXIT_POS)
        assert game.mode == "win"

    def test_exit_stays_locked_until_the_last_station(self, game):
        for sid in tq.STATION_ORDER[:-1]:
            solve(game, sid)
        assert tq.bfs_route(game, tq.EXIT_POS) is None

    def test_retry_after_gameover_resets_position_and_lives(self, game):
        game.lives = 1
        game.invuln = 0
        game._hit()
        assert game.mode == "gameover"
        game.retry()
        assert game.mode == "overworld"
        assert game.lives == 3
        assert (game.px, game.py) == tq.PLAYER_START


# ==========================================================================
# rendering — the fogged section-2 band
# ==========================================================================

class FakeScreen:
    """A minimal stand-in for a curses window: just enough for draw_base to
    run against and for a test to inspect what got written, at an exact
    size, with no real terminal needed."""

    def __init__(self, h=40, w=160):
        self.h, self.w = h, w
        self.rows = [[" "] * w for _ in range(h)]

    def erase(self):
        self.rows = [[" "] * self.w for _ in range(self.h)]

    def getmaxyx(self):
        return self.h, self.w

    def addstr(self, y, x, text, _style=0):
        # Mirrors real curses: addstr raises when the text would run past
        # the window's edges, rather than silently clipping. This is the
        # exact failure mode a shrunk-then-grown terminal hit in the wild
        # (addwstr() returned ERR) — draw_base must survive it, not crash.
        if y < 0 or y >= self.h or x < 0 or x + len(text) > self.w:
            raise curses.error("out of bounds")
        for i, ch in enumerate(text):
            self.rows[y][x + i] = ch

    def refresh(self):
        pass

    def dump(self):
        return "\n".join("".join(row) for row in self.rows)


class TestFoggedBandRendering:
    """Only the level the player is currently on (game.stage) is ever drawn.
    A level not yet reached, and a level already cleared, both read the same
    way: rendered as a solid sealed block with a "LEVEL n — SEALED" banner,
    never as a literal wall of repeated "??" (the old, confusing rendering)."""

    def test_fresh_game_shows_one_merged_seal_not_a_wall_of_question_marks(self, game):
        # Neither level 2 nor level 3 has been reached yet, so the whole
        # bottom band reads as ONE sealed area with a single banner — two
        # separate labels side by side read as a rendering glitch.
        assert game.stage == 1
        screen = FakeScreen()
        tq.draw_base(screen, game)
        dump = screen.dump()
        assert "LEVEL 2 — SEALED" in dump
        assert "LEVEL 3 — SEALED" not in dump
        assert "LEVEL 1 — SEALED" not in dump
        assert "??" not in dump

    def test_clearing_level_1_reveals_2_and_leaves_1_visible(self, game):
        # Level 1 never goes back under fog once you've played it.
        for sid in ("A", "B", "C", "D"):
            solve(game, sid)
        for coin_pos in tq.BAND0_COINS:
            if game.coins[coin_pos]:
                tq.walk_to(game, coin_pos)
        assert game.doors["hatch"]["locked"] is False
        assert game.stage == 2
        assert game.mode == "congrats"

        screen = FakeScreen()
        tq.draw_base(screen, game)
        dump = screen.dump()
        assert "LEVEL 1 — SEALED" not in dump
        assert "LEVEL 2 — SEALED" not in dump
        assert "LEVEL 3 — SEALED" in dump

    def test_clearing_level_2_reveals_3_and_leaves_1_and_2_visible(self, game):
        for sid in ("A", "B", "C", "D", "E", "F"):
            # entering E (and beyond) already collects every level-1 coin as
            # a side effect (see `enter`), so level 1 is cleared for free here
            solve(game, sid)
        for coin_pos in tq.BAND1_COINS:
            if game.coins[coin_pos]:
                tq.walk_to(game, coin_pos)
        game.dismiss_congrats()
        assert game.doors["doorFG"]["locked"] is False
        assert game.stage == 3

        screen = FakeScreen()
        tq.draw_base(screen, game)
        dump = screen.dump()
        assert "SEALED" not in dump

    def test_congrats_mode_shows_the_level_complete_message(self, game):
        for sid in ("A", "B", "C", "D"):
            solve(game, sid)
        for coin_pos in tq.BAND0_COINS:
            if game.coins[coin_pos]:
                tq.walk_to(game, coin_pos)
        assert game.mode == "congrats"
        assert "LEVEL 1" in game.congrats_text
        screen = FakeScreen()
        tq.draw_congrats(screen, game)
        dump = screen.dump()
        assert "LEVEL 1" in dump
        assert "PRESS SPACE" in dump

        game.dismiss_congrats()
        assert game.mode == "overworld"

    def test_winning_leaves_every_level_visible(self, game):
        for sid in tq.STATION_ORDER:
            solve(game, sid)
            if sid == "C":
                for coin_pos in tq.BAND0_COINS:
                    if game.coins[coin_pos]:
                        tq.walk_to(game, coin_pos)
                game.dismiss_congrats()
            if sid == "E":
                for coin_pos in tq.BAND1_COINS:
                    if game.coins[coin_pos]:
                        tq.walk_to(game, coin_pos)
                game.dismiss_congrats()
        tq.collect_all_coins(game)
        tq.walk_to(game, tq.EXIT_POS)
        assert game.mode == "win"
        assert game.stage == len(tq.STAGES) + 1

        screen = FakeScreen()
        tq.draw_base(screen, game)
        dump = screen.dump()
        assert "SEALED" not in dump

    def test_win_screen_spells_out_a_banner_in_block_letters(self, game):
        screen = FakeScreen()
        tq.draw_win(screen, game)
        dump = screen.dump()
        assert "SIGNAL RESTORED" in dump
        # The block-letter banner is drawn as several rows of '#' shapes,
        # not literal text — just check the art actually got drawn.
        assert "#" in dump
        assert ":wq" in dump


# ==========================================================================
# the ":wq" quit-from-anywhere easter egg
# ==========================================================================

class TestQuitCombo:
    """`feed_quit_combo` is the (curses-free) matching logic behind typing
    ":wq" to quit from anywhere except an active terminal prompt, where
    those are just ordinary command characters."""

    def _type(self, text, mode="overworld"):
        buf = ""
        matched = False
        for ch in text:
            buf, matched = tq.feed_quit_combo(buf, ord(ch), mode)
        return buf, matched

    def test_typing_the_sequence_one_key_at_a_time_matches(self):
        buf, matched = self._type(":wq")
        assert matched is True

    def test_a_partial_sequence_does_not_match(self):
        buf, matched = self._type(":w")
        assert matched is False

    def test_the_sequence_matches_even_with_leading_junk(self):
        # only the last few keys typed matter, not everything since launch
        buf, matched = self._type("hello :wq")
        assert matched is True

    def test_almost_the_sequence_does_not_match(self):
        for text in (":qw", "wq:", ":wqq", "wq"):
            _, matched = self._type(text)
            assert matched is False, text

    def test_does_not_match_while_actually_typing_at_a_terminal_prompt(self):
        # a real command could legitimately contain these characters —
        # this easter egg must never fire while the player is typing one
        buf, matched = self._type(":wq", mode="terminal")
        assert matched is False

    def test_idle_ticks_between_keystrokes_do_not_break_the_combo(self):
        # Real bug: curses returns -1 every 100ms when no key is pressed, so
        # typing ":wq" at human speed was resetting the buffer and could
        # never match.
        buf = ""
        for ch in (ord(":"), -1, -1, ord("w"), -1, ord("q")):
            buf, matched = tq.feed_quit_combo(buf, ch, "win")
        assert matched

    @staticmethod
    def _run_wait_for_quit(keys):
        """Drive wait_for_quit with a scripted key stream; returns True if it
        returned (quit) before the keys ran out."""
        stream = iter(keys)

        class Scr:
            def getch(self):
                try:
                    return next(stream)
                except StopIteration:
                    raise RuntimeError("out of keys")

        try:
            tq.wait_for_quit(Scr())
            return True
        except RuntimeError:
            return False

    def test_wq_then_enter_quits_even_with_idle_ticks_between_keys(self):
        idle = -1
        keys = [idle, idle, ord(":"), idle, idle, ord("w"), idle, ord("q"), idle, 10]
        assert self._run_wait_for_quit(keys)

    def test_wq_without_enter_does_not_quit_yet(self):
        assert not self._run_wait_for_quit([ord(":"), ord("w"), ord("q"), -1, -1])

    def test_enter_alone_does_not_quit(self):
        assert not self._run_wait_for_quit([10, 10, 13])

    def test_a_key_between_wq_and_enter_cancels_it(self):
        assert not self._run_wait_for_quit([ord(":"), ord("w"), ord("q"), ord("x"), 10])

    def test_keypad_enter_also_confirms(self):
        import curses
        assert self._run_wait_for_quit([ord(":"), ord("w"), ord("q"), curses.KEY_ENTER])

    def test_a_special_key_like_an_arrow_resets_the_buffer(self):
        buf, _ = tq.feed_quit_combo("", ord(":"), "overworld")
        buf, _ = tq.feed_quit_combo(buf, ord("w"), "overworld")
        # an arrow key (well outside the 0-255 typed-character range)
        # arrives mid-sequence — it should reset progress, not be ignored
        buf, matched = tq.feed_quit_combo(buf, curses.KEY_UP, "overworld")
        assert buf == ""
        assert matched is False
        buf, matched = tq.feed_quit_combo(buf, ord("q"), "overworld")
        assert matched is False  # "q" alone, progress was lost


# ==========================================================================
# rendering — surviving a too-small or resized terminal
# ==========================================================================

class TestSurvivesUndersizedTerminal:
    """A real user hit `_curses.error: addwstr() returned ERR` by shrinking
    the terminal below the game's minimum size and then growing it back —
    draw_base wrote the title/HUD lines with a bare stdscr.addstr, which
    raises instead of clipping when the window is too narrow or too short.
    Every draw_* function must tolerate a too-small window without an
    uncaught exception; the game should show the "please resize" screen
    instead of crashing."""

    SIZES = [
        (40, 160),   # comfortably large — the happy path
        (5, 20),     # far too small in both dimensions
        (28, 50),    # tall enough, too narrow (this shape triggered the bug)
        (10, 112),   # wide enough, too short
    ]

    @pytest.mark.parametrize("h,w", SIZES)
    def test_draw_base_never_raises(self, game, h, w):
        screen = FakeScreen(h=h, w=w)
        tq.draw_base(screen, game)  # must not raise curses.error

    @pytest.mark.parametrize("h,w", SIZES)
    def test_draw_gameover_never_raises(self, game, h, w):
        screen = FakeScreen(h=h, w=w)
        tq.draw_gameover(screen, game)

    @pytest.mark.parametrize("h,w", SIZES)
    def test_draw_win_never_raises(self, game, h, w):
        screen = FakeScreen(h=h, w=w)
        tq.draw_win(screen, game)

    @pytest.mark.parametrize("h,w", SIZES)
    def test_draw_intro_never_raises(self, game, h, w):
        screen = FakeScreen(h=h, w=w)
        tq.draw_intro(screen, game)

    @pytest.mark.parametrize("h,w", SIZES)
    def test_draw_congrats_never_raises(self, game, h, w):
        game.congrats_text = "LEVEL 1 COMPLETE!\n\nWell played."
        screen = FakeScreen(h=h, w=w)
        tq.draw_congrats(screen, game)

    def test_draw_too_small_shows_a_message_and_never_raises(self):
        screen = FakeScreen(h=10, w=60)  # small, but wide enough for the message
        tq.draw_too_small(screen)
        assert "resize" in screen.dump().lower()

    def test_draw_too_small_never_raises_even_when_tiny(self):
        screen = FakeScreen(h=5, w=20)
        tq.draw_too_small(screen)  # must not raise, even if the text gets cut


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
