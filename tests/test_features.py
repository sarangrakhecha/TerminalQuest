"""Tests for: the [▶] player marker, learn mode (enemies off), station
coaching tips, map cues (terminal letters / next-station), the quit prompt,
the solve bell, the exit cheat sheet, the CI workflow, plus gap-fillers for
older code paths."""
import curses
import os
import random
import runpy
import subprocess
import sys

import pytest

import terminalquest as tq
from test_game import FakeScreen, enter, solve
from test_ui import ScriptedScreen, fake_curses, qgame, restore_color_globals, run_main  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX, OY = 1, 3  # the map's origin on screen, as in draw_base


class StyledScreen(FakeScreen):
    """FakeScreen that also remembers the style each cell was drawn with."""

    def __init__(self, h=40, w=160):
        super().__init__(h, w)
        self.styles = {}

    def addstr(self, y, x, text, style=0):
        super().addstr(y, x, text, style)
        for i in range(len(text)):
            self.styles[(y, x + i)] = style


def draw(game, cls=FakeScreen, size=(40, 160)):
    s = cls(*size)
    tq.draw_base(s, game)
    return s


# ---- the [▶] player marker ---------------------------------------------------------

class TestPlayerMarker:
    def _marker(self, screen, game):
        row = OY + game.py
        start = OX + game.px * tq.CELL_W - 1
        return "".join(screen.rows[row][start:start + 3])

    @pytest.mark.parametrize("facing,glyph", [("up", "▲"), ("down", "▼"), ("left", "◀"), ("right", "▶")])
    def test_the_arrow_sits_between_brackets_and_points_where_you_face(self, qgame, facing, glyph):
        qgame.facing = facing
        assert self._marker(draw(qgame), qgame) == f"[{glyph}]"

    def test_the_legend_shows_the_bracketed_marker(self):
        assert tq.LEGEND.startswith("[▶] you")

    def test_a_bracketed_player_at_the_left_edge_does_not_crash(self, qgame):
        qgame.px = 0
        draw(qgame)

    def test_the_marker_survives_the_invulnerable_blink(self, qgame):
        qgame.invuln = 2
        assert self._marker(draw(qgame), qgame).startswith("[")


# ---- learn mode: enemies off ------------------------------------------------------------

class TestLearnMode:
    def _positions(self, g):
        return [(e["x"], e["y"]) for e in g.enemies]

    def test_enemies_freeze_when_switched_off(self, tmp_path):
        g = tq.Game(str(tmp_path / "a"), reset=True, quiz=False, enemies=False)
        before = self._positions(g)
        for _ in range(80):
            g.tick()
        assert self._positions(g) == before

    def test_enemies_move_when_on(self, tmp_path):
        g = tq.Game(str(tmp_path / "b"), reset=True, quiz=False)
        before = self._positions(g)
        for _ in range(80):
            g.tick()
        assert self._positions(g) != before

    def test_walking_into_an_enemy_costs_a_life_only_when_they_are_on(self, tmp_path):
        for enemies, expected in ((True, 2), (False, 3)):
            g = tq.Game(str(tmp_path / f"c{enemies}"), reset=True, quiz=False, enemies=enemies)
            e = g.enemies[0]
            g.px, g.py = e["x"] + 1, e["y"]
            g.invuln = 0
            g.try_move(-1, 0)
            assert g.lives == expected

    def test_enemies_are_not_drawn_when_off(self, tmp_path):
        g = tq.Game(str(tmp_path / "d"), reset=True, quiz=False, enemies=False)
        e = g.enemies[0]
        dump = draw(g).dump()
        row = dump.split("\n")[OY + e["y"]]
        assert row[OX + e["x"] * tq.CELL_W] not in ("x", "+")

    def test_toggle_flips_and_reports(self, qgame):
        qgame.toggle_enemies()
        assert qgame.enemies_on is False and "OFF" in qgame.message
        qgame.toggle_enemies()
        assert qgame.enemies_on is True and "ON" in qgame.message

    def test_the_hud_shows_the_enemy_setting(self, qgame):
        assert "enemies on" in draw(qgame).dump()
        qgame.toggle_enemies()
        assert "enemies off" in draw(qgame).dump()

    def test_e_key_toggles_in_the_real_loop(self, fake_curses, monkeypatch, qgame):
        run_main(monkeypatch, qgame, [" ", "e", "q", "y"])
        assert qgame.enemies_on is False

    def test_the_no_enemies_flag_reaches_the_game(self, fake_curses, monkeypatch, qgame):
        seen = {}
        monkeypatch.setattr(sys, "argv", ["terminalquest.py", "--no-enemies"])
        monkeypatch.setattr(tq, "Game", lambda *a, **k: (seen.update(k), qgame)[1])
        tq.main(ScriptedScreen([" ", "q", "y"]))
        assert seen["enemies"] is False

    def test_enemies_default_to_on(self, fake_curses, monkeypatch, qgame):
        seen = {}
        monkeypatch.setattr(sys, "argv", ["terminalquest.py"])
        monkeypatch.setattr(tq, "Game", lambda *a, **k: (seen.update(k), qgame)[1])
        tq.main(ScriptedScreen([" ", "q", "y"]))
        assert seen["enemies"] is True


# ---- coaching tips ---------------------------------------------------------------------------

class TestCoaching:
    def _submit(self, game, line):
        game.input_buf = line
        game.terminal_submit()

    def test_coach_tip_for_the_right_command_with_wrong_names(self, qgame):
        st = qgame.stations["F"]
        assert "right command" in tq.coach_tip(st, "cp wrong.txt other.txt")

    def test_coach_tip_for_a_typo_suggests_the_command(self, qgame):
        st = qgame.stations["F"]
        assert "did you mean `cp`" in tq.coach_tip(st, "cpp a b")

    def test_coach_tip_for_an_unrelated_command_points_at_the_panel(self, qgame):
        st = qgame.stations["F"]
        tip = tq.coach_tip(st, "banana split")
        assert "needs `cp`" in tip and "COMMAND box" in tip

    def test_coach_tip_for_a_blank_line_is_empty(self, qgame):
        assert tq.coach_tip(qgame.stations["F"], "   ") == ""

    def test_no_tip_after_one_miss_but_one_after_two(self, qgame):
        enter(qgame, "C")
        st = qgame.stations["C"]
        self._submit(qgame, "banana")
        assert st.fails == 1 and st.tip == ""
        self._submit(qgame, "banana")
        assert st.fails == 2 and "`cd`" in st.tip

    def test_the_tip_clears_when_the_station_is_solved(self, qgame):
        enter(qgame, "C")
        st = qgame.stations["C"]
        self._submit(qgame, "banana")
        self._submit(qgame, "banana")
        self._submit(qgame, "cd vault")
        assert st.solved and st.tip == ""

    def test_the_tip_updates_with_each_new_miss(self, qgame):
        enter(qgame, "C")
        st = qgame.stations["C"]
        self._submit(qgame, "banana")
        self._submit(qgame, "banana")
        first = st.tip
        self._submit(qgame, "cd valt")
        assert st.tip != first and "right command" in st.tip

    def test_an_empty_submit_is_not_a_miss(self, qgame):
        enter(qgame, "C")
        self._submit(qgame, "   ")
        assert qgame.stations["C"].fails == 0

    def test_create_stations_keep_their_specific_nudge_over_the_generic_tip(self, qgame):
        enter(qgame, "D")
        st = qgame.stations["D"]
        self._submit(qgame, "mkdir mystuff")
        self._submit(qgame, "mkdir mystuff2")
        assert st.tip == ""
        assert "this mission needs" in st.transcript[-1][1]

    def test_the_tip_is_drawn_in_the_panel(self, qgame):
        enter(qgame, "C")
        self._submit(qgame, "banana")
        self._submit(qgame, "banana")
        s = FakeScreen()
        tq.draw_terminal(s, qgame)
        assert "tip:" in s.dump()

    def test_a_tip_never_costs_lives_or_blocks_progress(self, qgame):
        enter(qgame, "C")
        lives = qgame.lives
        for _ in range(5):
            self._submit(qgame, "banana")
        self._submit(qgame, "cd vault")
        assert qgame.lives == lives and qgame.stations["C"].solved


# ---- map cues -----------------------------------------------------------------------------------

class TestMapCues:
    def test_computers_are_labeled_with_their_station_letter(self, qgame):
        qgame.stage = 3  # reveal every level
        dump = draw(qgame).dump()
        for sid in tq.STATION_ORDER:
            assert f"▣{sid}" in dump

    def test_fogged_levels_do_not_leak_their_letters(self, qgame):
        dump = draw(qgame).dump()
        assert "▣A" in dump and "▣H" not in dump

    def test_a_solved_terminal_is_dimmed(self, qgame):
        pos = next(p for p, sid in tq.COMPUTERS.items() if sid == "A")
        qgame.px, qgame.py = tq.PLAYER_START  # keep the marker off the terminal tile
        before = draw(qgame, StyledScreen).styles[(OY + pos[1], OX + pos[0] * tq.CELL_W)]
        solve(qgame, "A")
        qgame.px, qgame.py = tq.PLAYER_START
        after = draw(qgame, StyledScreen).styles[(OY + pos[1], OX + pos[0] * tq.CELL_W)]
        assert not before & curses.A_DIM and after & curses.A_DIM

    def test_next_station_walks_the_order(self, qgame):
        assert qgame.next_station() == "A"
        solve(qgame, "A")
        assert qgame.next_station() == "B"
        for sid in tq.STATION_ORDER:
            qgame.stations[sid].solved = True
        assert qgame.next_station() is None

    def test_the_hud_names_the_next_terminal_then_points_at_the_exit(self, qgame):
        assert "next: terminal A" in draw(qgame).dump()
        for sid in tq.STATION_ORDER:
            qgame.stations[sid].solved = True
        assert "head for X" in draw(qgame).dump()


# ---- quit confirmation ----------------------------------------------------------------------------

class TestQuitPrompt:
    def test_ask_and_cancel(self, qgame):
        qgame.ask_quit()
        assert qgame.mode == "quitconfirm"
        qgame.cancel_quit()
        assert qgame.mode == "overworld"

    @pytest.mark.parametrize("mode", ["terminal", "sign", "congrats", "gameover", "win"])
    def test_ask_quit_only_works_from_the_map(self, qgame, mode):
        qgame.mode = mode
        qgame.ask_quit()
        assert qgame.mode == mode

    def test_cancel_quit_outside_the_prompt_does_nothing(self, qgame):
        qgame.mode = "sign"
        qgame.cancel_quit()
        assert qgame.mode == "sign"

    def test_q_then_n_keeps_playing(self, fake_curses, monkeypatch, qgame):
        run_main(monkeypatch, qgame, [" ", "q", "n", "q", "y"])

    def test_q_then_escape_keeps_playing(self, fake_curses, monkeypatch, qgame):
        run_main(monkeypatch, qgame, [" ", "q", 27, "q", "y"])

    def test_other_keys_at_the_prompt_do_nothing(self, fake_curses, monkeypatch, qgame):
        run_main(monkeypatch, qgame, [" ", "q", "x", curses.KEY_UP, -1, "y"])

    def test_q_alone_no_longer_quits(self, fake_curses, monkeypatch, qgame):
        with pytest.raises(AssertionError, match="kept running"):
            run_main(monkeypatch, qgame, [" ", "q"])

    def test_the_prompt_says_what_the_keys_do(self, qgame):
        qgame.ask_quit()
        s = FakeScreen()
        tq.draw_quitconfirm(s, qgame)
        dump = s.dump()
        assert "QUIT" in dump and "Y = quit" in dump and "N = keep playing" in dump

    @pytest.mark.parametrize("size", [(40, 160), (20, 60), (8, 30)])
    def test_the_prompt_draws_at_any_size(self, qgame, size):
        qgame.ask_quit()
        tq.draw_quitconfirm(FakeScreen(*size), qgame)


# ---- the solve bell ------------------------------------------------------------------------------------

class TestBell:
    @pytest.fixture
    def beeps(self, monkeypatch):
        count = {"n": 0}
        monkeypatch.setattr(curses, "beep", lambda: count.__setitem__("n", count["n"] + 1))
        return count

    def test_solving_a_station_sets_the_bell(self, qgame):
        solve(qgame, "A")
        assert qgame.bell is True

    def test_a_wrong_command_does_not(self, qgame):
        enter(qgame, "A")
        qgame.input_buf = "frobnicate"
        qgame.terminal_submit()
        assert qgame.bell is False

    def test_the_loop_rings_once_per_solve(self, fake_curses, monkeypatch, qgame, beeps):
        qgame.enter_terminal("A")
        run_main(monkeypatch, qgame, [" ", "l", "s", 10, "z", "q", "y"])
        assert beeps["n"] == 1 and qgame.bell is False

    def test_no_bell_when_sound_is_off(self, fake_curses, monkeypatch, qgame, beeps):
        qgame.sound_on = False
        qgame.enter_terminal("A")
        run_main(monkeypatch, qgame, [" ", "l", "s", 10, "z", "q", "y"])
        assert beeps["n"] == 0 and qgame.bell is False

    def test_a_terminal_that_cannot_beep_is_survived(self, fake_curses, monkeypatch, qgame):
        def boom():
            raise curses.error("no bell")
        monkeypatch.setattr(curses, "beep", boom)
        qgame.enter_terminal("A")
        run_main(monkeypatch, qgame, [" ", "l", "s", 10, "z", "q", "y"])

    def test_b_toggles_sound(self, fake_curses, monkeypatch, qgame):
        run_main(monkeypatch, qgame, [" ", "b", "q", "y"])
        assert qgame.sound_on is False and "OFF" in qgame.message
        qgame.toggle_sound()
        assert qgame.sound_on is True and "ON" in qgame.message

    def test_the_hud_shows_the_sound_setting(self, qgame):
        assert "sound on" in draw(qgame).dump()
        qgame.toggle_sound()
        assert "sound off" in draw(qgame).dump()


# ---- the cheat sheet -------------------------------------------------------------------------------------------

class TestCheatSheet:
    def test_it_lists_all_eight_commands_with_examples(self):
        text = tq.cheat_sheet_text()
        for word in ("ls", "cat notes.txt", "cd projects", "mkdir photos", "touch todo.txt",
                     "cp a.txt b.txt", "mv old.txt new.txt", "rm junk.tmp"):
            assert word in text
        assert len(tq.CHEAT_SHEET) == 8

    def test_examples_match_the_commands_the_game_teaches(self):
        assert [row[3] for row in tq.CHEAT_SHEET] == [first for first in
                                                       ("ls", "cat", "cd", "mkdir", "touch", "cp", "mv", "rm")]
        for word, example, _, key in tq.CHEAT_SHEET:
            assert example.split()[0] == key == word

    def test_only_commands_you_used_are_ticked(self):
        text = tq.cheat_sheet_text(["ls", "rm"])
        ticked = [ln for ln in text.splitlines() if "✓" in ln]
        assert len(ticked) == 2 and any("ls" in ln for ln in ticked) and any("rm" in ln for ln in ticked)

    def test_nothing_is_ticked_when_nothing_was_learned(self):
        assert "✓" not in tq.cheat_sheet_text()

    def test_main_remembers_the_game_for_the_exit_sheet(self, fake_curses, monkeypatch, qgame):
        run_main(monkeypatch, qgame, [" ", "q", "y"])
        assert tq.LAST_GAME is qgame

    def test_running_the_script_prints_the_sheet_after_the_game_closes(self, monkeypatch, capsys):
        monkeypatch.setattr(curses, "wrapper", lambda fn: None)
        monkeypatch.setattr(sys, "argv", ["terminalquest.py"])
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(os.path.join(ROOT, "terminalquest.py"), run_name="__main__")
        assert exc.value.code == 0
        assert "cheat sheet" in capsys.readouterr().out

    def test_running_the_script_with_selftest_still_works(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["terminalquest.py", "--selftest"])
        runpy.run_path(os.path.join(ROOT, "terminalquest.py"), run_name="__main__")
        assert "SELFTEST PASSED" in capsys.readouterr().out


# ---- CI workflow -------------------------------------------------------------------------------------------------------

class TestCiWorkflow:
    @pytest.fixture
    def workflow(self):
        with open(os.path.join(ROOT, ".github", "workflows", "tests.yml")) as f:
            return f.read()

    def test_it_runs_the_tests_the_selftest_and_a_coverage_floor(self, workflow):
        assert "pytest" in workflow and "--selftest" in workflow
        assert "--cov-fail-under=90" in workflow

    def test_it_covers_macos_and_linux_and_several_pythons(self, workflow):
        for token in ("ubuntu-latest", "macos-latest", "3.9", "3.12"):
            assert token in workflow

    def test_the_dev_requirements_include_pytest_cov(self):
        with open(os.path.join(ROOT, "requirements-dev.txt")) as f:
            assert "pytest-cov" in f.read()

    def test_the_readme_has_the_badge(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            assert "actions/workflows/tests.yml/badge.svg" in f.read()


# ---- gap-fillers for older code paths ---------------------------------------------------------------------------------------

class TestOlderGaps:
    def test_a_command_that_hangs_is_stopped_with_a_friendly_message(self, tmp_path, monkeypatch):
        def hang(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=10)
        monkeypatch.setattr(subprocess, "run", hang)
        cwd, out, err = tq.run_command("sleep 99", str(tmp_path), str(tmp_path))
        assert cwd == str(tmp_path) and "too long" in err

    def test_write_can_set_file_permissions(self, tmp_path):
        target = tmp_path / "sub" / "f.txt"
        tq._write(str(target), "hi", mode=0o600)
        assert target.read_text() == "hi" and (target.stat().st_mode & 0o777) == 0o600

    @pytest.mark.parametrize("sid", tq.STATION_ORDER)
    def test_a_solved_station_stays_solved_when_checked_again(self, qgame, sid):
        solve(qgame, sid)
        st = qgame.stations[sid]
        qgame._checkers[sid](st)
        assert st.solved

    def test_bfs_route_to_where_you_already_stand_is_empty(self, qgame):
        assert tq.bfs_route(qgame, (qgame.px, qgame.py)) == []

    def test_bfs_route_to_a_wall_is_none(self, qgame):
        assert tq.bfs_route(qgame, (0, 0)) is None

    def test_walk_to_stops_when_a_sign_takes_over(self, qgame):
        sign = next(iter(tq.SIGNS))
        tq.walk_to(qgame, sign)
        assert qgame.mode == "sign"
        tq.walk_to(qgame, (qgame.px, qgame.py))  # no crash, no movement

    def test_moving_is_ignored_outside_the_map(self, qgame):
        qgame.mode = "terminal"
        before = (qgame.px, qgame.py)
        qgame.try_move(1, 0)
        assert (qgame.px, qgame.py) == before

    def test_an_empty_terminal_submission_changes_nothing(self, qgame):
        enter(qgame, "A")
        qgame.input_buf = "   "
        qgame.terminal_submit()
        assert qgame.stations["A"].attempts == 0 and qgame.stations["A"].transcript == []

    def test_the_solved_countdown_returns_you_to_the_map(self, qgame):
        solve(qgame, "A")
        qgame.enter_terminal("A")
        assert qgame.return_timer > 0
        for _ in range(qgame.return_timer + 1):
            qgame.tick_terminal()
        assert qgame.mode == "overworld"

    def test_stage_advance_is_a_noop_once_every_level_is_cleared(self, qgame):
        qgame.stage = len(tq.STAGES) + 1
        qgame._maybe_advance_stage()
        assert qgame.mode == "overworld"

    def test_quiz_calls_out_of_order_are_ignored(self, qgame):
        qgame.quiz_skip()  # no quiz active
        qgame.quiz_start()  # not at an offer
        assert qgame.mode == "overworld"
        qgame.begin_quiz_offer(1, "overworld")
        qgame.quiz_submit(0)  # still at the offer, not a question
        qgame.quiz_continue()
        assert qgame.mode == "quizoffer"

    def test_quiz_ignores_a_missing_or_out_of_range_choice(self, tmp_path):
        g = tq.Game(str(tmp_path / "q"), reset=True, rng=random.Random(5))
        g.begin_quiz_offer(1, "overworld")
        g.quiz_start()
        g.quiz["items"][0] = dict(tq.QUIZ_POOLS[1][4])
        g.quiz_submit(None)
        g.quiz_submit(99)
        assert g.quiz["attempts"] == 0

    def test_quiz_ignores_a_submit_during_feedback(self, tmp_path):
        g = tq.Game(str(tmp_path / "q2"), reset=True, rng=random.Random(5))
        g.begin_quiz_offer(1, "overworld")
        g.quiz_start()
        g.quiz["phase"] = "feedback"
        g.quiz_submit(0)
        assert g.quiz["phase"] == "feedback"

    def test_quiz_continue_is_ignored_while_still_asking(self, tmp_path):
        g = tq.Game(str(tmp_path / "q3"), reset=True, rng=random.Random(5))
        g.begin_quiz_offer(1, "overworld")
        g.quiz_start()
        g.quiz_continue()
        assert g.quiz["idx"] == 0

    def test_first_meaningful_line_of_nothing(self):
        assert "no output" in tq._first_meaningful_line("\n  \n")

    def test_the_win_screen_lists_the_commands_you_used(self, qgame):
        qgame.mode = "win"
        qgame.learned = ["ls", "cat"]
        s = FakeScreen()
        tq.draw_win(s, qgame)
        assert "ls, cat" in s.dump()

    def test_the_press_any_key_sign_draws(self, qgame):
        qgame.mode, qgame.sign_text = "sign", "a sign\n\nwith paragraphs"
        s = FakeScreen()
        tq.draw_sign(s, qgame)
        assert "with paragraphs" in s.dump()

    @pytest.mark.parametrize("size", [(6, 20), (10, 40), (14, 50)])
    def test_every_screen_survives_a_tiny_window(self, qgame, size):
        enter(qgame, "A")
        qgame.input_buf = "x" * 60
        qgame.terminal_submit()
        for fn, mode in ((tq.draw_terminal, "terminal"), (tq.draw_sign, "sign"), (tq.draw_win, "win"),
                         (tq.draw_quitconfirm, "quitconfirm"), (tq.draw_gameover, "gameover")):
            qgame.mode = mode
            if mode == "sign":
                qgame.sign_text = "hello"
            if mode == "terminal":
                qgame.active_station = "A"
            fn(FakeScreen(*size), qgame)
        tq.draw_too_small(FakeScreen(*size))

    def test_a_terminal_that_rejects_color_setup_still_starts(self, monkeypatch, qgame):
        def boom():
            raise curses.error("no color")
        monkeypatch.setattr(curses, "curs_set", lambda n: None)
        monkeypatch.setattr(curses, "start_color", boom)
        monkeypatch.setattr(tq, "Game", lambda *a, **k: qgame)
        tq.main(ScriptedScreen([" ", "q", "y"]))


class TestIntro:
    def test_the_intro_waits_for_a_real_keypress_not_an_idle_tick(self, fake_curses, monkeypatch, qgame):
        # Real bug found by playing the game in a real terminal: getch() gives -1
        # every 100ms when nothing is pressed, and the intro treated that as
        # "any key", flashing for a tenth of a second.  Here the "q" must be
        # consumed as the start key, so the script runs out while main() is
        # still looping, rather than quitting.
        with pytest.raises(AssertionError, match="kept running"):
            run_main(monkeypatch, qgame, [-1, -1, -1, "q", "y"])

    def test_any_real_key_starts_the_game(self, fake_curses, monkeypatch, qgame):
        run_main(monkeypatch, qgame, [-1, -1, "z", "q", "y"])


# ---- real-terminal QA findings: safety guard, clean exits, Esc delay --------------------------------

@pytest.fixture
def box(tmp_path):
    """A game-folder-shaped sandbox with a sibling 'outside' folder to protect."""
    root = tmp_path / "game" / "stationX"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "PRECIOUS.txt").write_text("keep me")
    return str(root), str(outside)


class TestPathGuard:
    @pytest.mark.parametrize("line", [
        "cat /etc/passwd", "ls /", "touch /tmp/tq_x", "rm /tmp/whatever", "cd /", "cd /tmp",
        "ls ..", "touch ../sibling.txt", "cat ../../etc/hosts", "cp a.txt /tmp/a.txt",
        "echo hi > /tmp/tq_out", "echo hi >/tmp/tq_out", "ls 2>/tmp/err", "FOO=/etc cat x",
        "rm -rf /*", "ls /bin", "/bin/ls", "cat \"/etc/passwd\"", "cat '/etc/passwd'",
    ])
    def test_reaching_outside_the_folder_is_refused(self, box, line):
        root, _ = box
        cwd, out, err = tq.run_command(line, root, root)
        assert err == tq.OUTSIDE_MSG or err == "nope. not even here.", (line, err)
        assert cwd == root and out == ""

    @pytest.mark.parametrize("line", [
        "ls", "ls .", "ls ./sub", "cat a.txt", "touch new.txt", "mkdir -p deep/er", "echo hi > out.txt",
        "cp a.txt b.txt", "mv a.txt c.txt", "rm a.txt", "ls -la", "ls 2>/dev/null", "echo 1/2",
        "echo \"quoted words\"", "cd ~", "ls ~", "echo $HOME", "cat a.txt | grep a", "ls; pwd", "cd sub && ls",
    ])
    def test_ordinary_commands_are_untouched(self, box, line):
        root, _ = box
        os.makedirs(os.path.join(root, "sub"), exist_ok=True)
        with open(os.path.join(root, "a.txt"), "w") as f:
            f.write("a\n")
        cwd, out, err = tq.run_command(line, root, root)
        assert err != tq.OUTSIDE_MSG and "nope" not in err, (line, err)

    def test_the_players_real_home_is_never_touched_by_rm_rf_tilde(self, box, tmp_path, monkeypatch):
        root, outside = box
        monkeypatch.setenv("HOME", outside)  # pretend this is the real home
        for line in ("rm -rf ~", "rm -rf $HOME", "rm -rf ${HOME}", "rm -rf ~/*", "rm -r -f ~", "rm -fr ~"):
            tq.run_command(line, root, root)
            assert os.path.exists(os.path.join(outside, "PRECIOUS.txt")), line

    def test_home_inside_the_game_is_the_game_folder(self, box, monkeypatch):
        root, outside = box
        monkeypatch.setenv("HOME", outside)
        cwd, out, err = tq.run_command("echo $HOME", root, root)
        assert out.strip() == os.path.realpath(root)

    def test_bare_cd_goes_to_the_game_folder_not_the_real_home(self, box, monkeypatch):
        root, outside = box
        monkeypatch.setenv("HOME", outside)
        os.makedirs(os.path.join(root, "sub"), exist_ok=True)
        cwd, _, _ = tq.run_command("cd sub", root, root)
        cwd2, _, _ = tq.run_command("cd", cwd, root)
        assert cwd2 == os.path.realpath(root)

    @pytest.mark.parametrize("line", ["rm -rf ~", "rm -rf .", "rm -r ..", "rm -rf $HOME", "rm -Rf ~", "rm --recursive ~"])
    def test_recursive_rm_of_the_folder_itself_or_its_parents_is_refused(self, box, line):
        root, _ = box
        os.makedirs(os.path.join(root, "sub"), exist_ok=True)
        cwd, out, err = tq.run_command(line, os.path.join(root, "sub"), root)
        assert err != "" and os.path.isdir(root), (line, err)

    def test_recursive_rm_of_a_subfolder_is_allowed(self, box):
        root, _ = box
        os.makedirs(os.path.join(root, "junkdir", "inner"))
        cwd, out, err = tq.run_command("rm -rf junkdir", root, root)
        assert err == "" and not os.path.exists(os.path.join(root, "junkdir"))

    def test_a_symlink_that_points_outside_is_refused(self, box):
        root, outside = box
        os.symlink(outside, os.path.join(root, "portal"))
        cwd, out, err = tq.run_command("cat portal/PRECIOUS.txt", root, root)
        assert err == tq.OUTSIDE_MSG and out == ""
        cwd, out, err = tq.run_command("touch portal/new.txt", root, root)
        assert err == tq.OUTSIDE_MSG and not os.path.exists(os.path.join(outside, "new.txt"))

    def test_unbalanced_quotes_are_left_for_bash_to_reject(self, box):
        root, _ = box
        cwd, out, err = tq.run_command("echo 'oops", root, root)
        assert err != tq.OUTSIDE_MSG and err != ""

    def test_the_dev_null_idiom_still_works(self, box):
        root, _ = box
        cwd, out, err = tq.run_command("ls nosuchfile 2>/dev/null", root, root)
        assert err != tq.OUTSIDE_MSG

    def test_writing_outside_leaves_no_file_behind(self, box):
        root, outside = box
        tq.run_command(f"touch {outside}/created.txt", root, root)
        assert not os.path.exists(os.path.join(outside, "created.txt"))

    def test_a_station_shows_the_friendly_outside_message(self, qgame):
        enter(qgame, "A")
        qgame.input_buf = "cat /etc/passwd"
        qgame.terminal_submit()
        assert tq.OUTSIDE_MSG in qgame.stations["A"].transcript[-1][1]

    def test_quiz_answers_still_grade_with_the_guard_on(self):
        for _, q in ((l, q) for l, pool in tq.QUIZ_POOLS.items() for q in pool if q["kind"] == "type"):
            assert tq.grade_typed(q, q["answer"])[0]

    @pytest.mark.parametrize("sid", tq.STATION_ORDER)
    def test_every_stations_solution_is_unaffected_by_the_guard(self, qgame, sid):
        solve(qgame, sid)
        assert qgame.stations[sid].solved


class TestCleanExits:
    def test_ctrl_c_exits_quietly_with_130_and_the_cheat_sheet(self, monkeypatch, capsys):
        def interrupted(fn):
            raise KeyboardInterrupt
        monkeypatch.setattr(curses, "wrapper", interrupted)
        assert tq.run() == 130
        out = capsys.readouterr()
        assert "cheat sheet" in out.out and "Traceback" not in out.err

    def test_a_normal_quit_returns_0(self, monkeypatch, capsys):
        monkeypatch.setattr(curses, "wrapper", lambda fn: None)
        assert tq.run() == 0
        assert "cheat sheet" in capsys.readouterr().out

    def test_no_terminal_gives_a_friendly_message_and_exit_code_1(self, monkeypatch, capsys):
        def no_tty(fn):
            raise curses.error("setupterm: could not find terminal")
        monkeypatch.setattr(curses, "wrapper", no_tty)
        assert tq.run() == 1
        err = capsys.readouterr().err
        assert "real terminal" in err and "112x28" in err and "could not find terminal" in err

    def test_esc_no_longer_waits_a_full_second(self, monkeypatch):
        monkeypatch.delenv("ESCDELAY", raising=False)
        monkeypatch.setattr(curses, "wrapper", lambda fn: None)
        tq.run()
        assert int(os.environ["ESCDELAY"]) <= 50

    def test_a_player_set_escdelay_is_respected(self, monkeypatch):
        monkeypatch.setenv("ESCDELAY", "300")
        monkeypatch.setattr(curses, "wrapper", lambda fn: None)
        tq.run()
        assert os.environ["ESCDELAY"] == "300"
