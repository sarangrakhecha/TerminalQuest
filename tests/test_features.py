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
        runpy.run_path(os.path.join(ROOT, "terminalquest.py"), run_name="__main__")
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
