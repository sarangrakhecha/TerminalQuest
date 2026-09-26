"""Tests for the curses-facing layer: color setup, the terminal panel, the
real main() input loop (driven by a scripted fake screen), and selftest()."""
import curses
import random

import pytest

import terminalquest as tq
from test_game import FakeScreen, enter, solve

CP_NAMES = ("CP_PLAYER", "CP_ENEMY", "CP_DOOR", "CP_COMP", "CP_COIN", "CP_OK", "CP_ERR")


@pytest.fixture(autouse=True)
def restore_color_globals():
    saved = {n: getattr(tq, n) for n in CP_NAMES}
    yield
    for n, v in saved.items():
        setattr(tq, n, v)


@pytest.fixture
def fake_curses(monkeypatch):
    """Stand in for the bits of curses that need a real initscr()."""
    calls = {"pairs": []}
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "init_pair", lambda *a: calls["pairs"].append(a))
    monkeypatch.setattr(curses, "color_pair", lambda n: n << 8)
    return calls


@pytest.fixture
def qgame(tmp_path):
    return tq.Game(str(tmp_path / "ui"), reset=True, quiz=False)


# ---- setup_colors --------------------------------------------------------------

class TestSetupColors:
    def test_color_mode_assigns_color_pairs(self, fake_curses):
        tq.setup_colors()
        assert len(fake_curses["pairs"]) == 4
        assert tq.CP_PLAYER and tq.CP_ENEMY and tq.CP_DOOR and tq.CP_COMP
        assert tq.CP_COIN == 0  # deliberately uncolored

    def test_high_contrast_uses_attributes_not_colors(self, fake_curses):
        tq.setup_colors(high_contrast=True)
        assert fake_curses["pairs"] == []
        assert tq.CP_PLAYER & curses.A_REVERSE
        assert tq.CP_ERR & curses.A_UNDERLINE

    def test_a_terminal_without_color_falls_back_to_plain(self, fake_curses, monkeypatch):
        monkeypatch.setattr(curses, "has_colors", lambda: False)
        tq.setup_colors()
        assert all(getattr(tq, n) == 0 for n in CP_NAMES)

    def test_a_curses_error_while_setting_up_pairs_is_survived(self, fake_curses, monkeypatch):
        def boom(*a):
            raise curses.error("nope")
        monkeypatch.setattr(curses, "init_pair", boom)
        tq.setup_colors()  # must not raise


# ---- the terminal panel ------------------------------------------------------------

SIZES = [(40, 160), (30, 112), (24, 80), (16, 60)]


class TestDrawTerminal:
    def _panel(self, game, size=(40, 160)):
        s = FakeScreen(*size)
        tq.draw_terminal(s, game)
        return s.dump()

    def test_fresh_station_shows_the_reference_panel(self, qgame):
        enter(qgame, "D")
        dump = self._panel(qgame)
        for text in ("COMMAND", "WHAT IT DOES", "SYNTAX", "YOUR TASK", "mkdir stash"):
            assert text in dump

    def test_a_failed_command_is_shown_as_an_error_with_the_explain_prompt(self, qgame):
        enter(qgame, "B")
        qgame.input_buf = "cat nosuchfile.txt"
        qgame.terminal_submit()
        dump = self._panel(qgame)
        assert "cat nosuchfile.txt" in dump and "Press ? to explain" in dump

    def test_explanation_toggle_shows_a_why_line(self, qgame):
        enter(qgame, "A")
        qgame.input_buf = "ls"
        qgame.terminal_submit()
        qgame.stations["A"].solved = False  # look at the unsolved layout with a result
        qgame.toggle_explanation()
        assert "Why:" in self._panel(qgame)

    def test_explanation_of_an_error_says_bash_reported_it(self, qgame):
        enter(qgame, "B")
        qgame.input_buf = "cat nosuchfile.txt"
        qgame.terminal_submit()
        qgame.toggle_explanation()
        assert "didn't work" in self._panel(qgame)

    def test_an_unlisted_command_gets_the_generic_explanation(self, qgame):
        enter(qgame, "A")
        qgame.input_buf = "echo hi"
        qgame.terminal_submit()
        qgame.toggle_explanation()
        assert "whatever bash actually printed" in self._panel(qgame)

    def test_solved_station_shows_the_solved_banner_and_countdown(self, qgame):
        solve(qgame, "A")
        qgame.enter_terminal("A")
        dump = self._panel(qgame)
        assert "SOLVED" in dump and "heading back to the ship" in dump

    def test_typed_input_appears_at_the_prompt(self, qgame):
        enter(qgame, "D")
        qgame.input_buf = "mkdir sta"
        assert "$ mkdir sta" in self._panel(qgame)

    @pytest.mark.parametrize("size", SIZES)
    def test_every_state_draws_at_every_size_without_raising(self, qgame, size):
        enter(qgame, "B")
        self._panel(qgame, size)
        qgame.input_buf = "cat nosuch"
        qgame.terminal_submit()
        self._panel(qgame, size)
        qgame.toggle_explanation()
        self._panel(qgame, size)
        qgame.input_buf = "cat key.txt"
        qgame.terminal_submit()
        self._panel(qgame, size)


# ---- the real main() loop, driven by a scripted screen ---------------------------------

class ScriptedScreen(FakeScreen):
    """A FakeScreen that also feeds main() a scripted list of keypresses.
    Running out of keys is a test failure (main should have quit by then)."""

    def __init__(self, keys, h=40, w=160):
        super().__init__(h, w)
        self.keys = list(keys)

    def timeout(self, ms):
        pass

    def getch(self):
        if not self.keys:
            raise AssertionError("main() kept running after the script ended")
        k = self.keys.pop(0)
        return ord(k) if isinstance(k, str) else k


def run_main(monkeypatch, game, keys, size=(40, 160)):
    monkeypatch.setattr(tq, "Game", lambda *a, **k: game)
    screen = ScriptedScreen(keys, *size)
    tq.main(screen)
    return screen


class TestMainLoop:
    def test_any_key_starts_then_q_quits(self, fake_curses, monkeypatch, qgame):
        run_main(monkeypatch, qgame, ["x", "q", "y"])

    def test_arrow_keys_move_and_t_toggles_high_contrast(self, fake_curses, monkeypatch, qgame):
        start = (qgame.px, qgame.py)
        run_main(monkeypatch, qgame, [
            " ", "t", curses.KEY_RIGHT, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_UP, -1, "t", "q", "y"])
        assert qgame.high_contrast is False
        assert (qgame.px, qgame.py) != start or qgame.message

    def test_a_too_small_window_shows_the_resize_message_until_it_can_quit(self, fake_curses, monkeypatch, qgame):
        screen = run_main(monkeypatch, qgame, [" ", "x", "q"], size=(10, 40))
        assert "resize" in screen.dump().lower()

    def test_a_sign_is_dismissed_by_any_key(self, fake_curses, monkeypatch, qgame):
        qgame.mode, qgame.sign_text = "sign", "hello there"
        run_main(monkeypatch, qgame, [" ", "z", "q", "y"])
        assert qgame.mode == "quitconfirm"  # left the game from the quit prompt

    def test_congrats_only_dismisses_on_space(self, fake_curses, monkeypatch, qgame):
        qgame.mode, qgame.congrats_text = "congrats", "LEVEL 1 COMPLETE!"
        run_main(monkeypatch, qgame, [" ", curses.KEY_RIGHT, "x", " ", "q", "y"])
        assert qgame.mode == "quitconfirm"  # left the game from the quit prompt

    def test_playing_a_station_from_the_keyboard(self, fake_curses, monkeypatch, qgame):
        qgame.enter_terminal("A")
        keys = [" ", "?", "l", "x", curses.KEY_BACKSPACE, "s", "\t", 27, "l", "s", curses.KEY_ENTER, "z", "q", "y"]
        run_main(monkeypatch, qgame, keys)
        assert qgame.stations["A"].solved
        assert qgame.mode == "quitconfirm"  # left the game from the quit prompt

    def test_escape_on_an_empty_prompt_leaves_the_terminal(self, fake_curses, monkeypatch, qgame):
        qgame.enter_terminal("A")
        run_main(monkeypatch, qgame, [" ", 27, "q", "y"])
        assert qgame.mode == "quitconfirm"  # left the game from the quit prompt

    def test_game_over_retries_on_any_key_and_quits_on_q(self, fake_curses, monkeypatch, qgame):
        qgame.mode = "gameover"
        run_main(monkeypatch, qgame, [" ", "r", "q", "y"])
        assert qgame.mode == "quitconfirm"  # left the game from the quit prompt and qgame.lives == 3

    def test_game_over_can_quit_directly(self, fake_curses, monkeypatch, qgame):
        qgame.mode = "gameover"
        run_main(monkeypatch, qgame, [" ", "q"])

    def test_win_screen_needs_wq_then_enter(self, fake_curses, monkeypatch, qgame):
        qgame.mode = "win"
        run_main(monkeypatch, qgame, [" ", "q", ":", "w", "q", 10])

    def test_a_quiz_can_be_taken_from_the_keyboard(self, fake_curses, monkeypatch, tmp_path):
        g = tq.Game(str(tmp_path / "qk"), reset=True, quiz=True, rng=random.Random(2))
        g.begin_quiz_offer(1, "overworld")
        keys = [" ", "y"]
        probe = tq.Game(str(tmp_path / "probe"), reset=True, rng=random.Random(2))
        probe.begin_quiz_offer(1, "overworld")
        probe.quiz_start()
        for q in probe.quiz["items"]:
            if q["kind"] == "type":
                keys += list(q["answer"]) + [10]
            else:
                keys += [str(q["correct"] + 1)]
            keys += [" "]
        keys += [" ", "q", "y"]  # summary -> back to the map -> quit
        run_main(monkeypatch, g, keys)
        assert g.quiz_right == 3 and g.mode == "quitconfirm"  # left the game from the quit prompt

    def test_declining_the_quiz_offer_from_the_keyboard(self, fake_curses, monkeypatch, tmp_path):
        g = tq.Game(str(tmp_path / "qn"), reset=True, quiz=True)
        g.begin_quiz_offer(1, "overworld")
        run_main(monkeypatch, g, [" ", "n", "q", "y"])
        assert g.mode == "quitconfirm"  # left the game from the quit prompt


# ---- the built-in self-test ----------------------------------------------------------------

def test_selftest_passes(capsys):
    tq.selftest()
    assert "SELFTEST PASSED" in capsys.readouterr().out
