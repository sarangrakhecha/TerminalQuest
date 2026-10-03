"""Robustness: what happens when a player does something unexpected.

Three groups, all found by throwing hostile input at the real game:
  * command execution — binary output, a bare `cat`, runaway output, jobs that
    outlive their command, timeouts (each was a crash or a freeze);
  * the redesigned renderer — fog vs walls, animation, HUD, popups, banners,
    and a differential check that batched map drawing paints exactly what
    drawing tile-by-tile would;
  * a seeded fuzzer that drives the real main() loop with random keys at
    random window sizes and must never raise.
"""
import curses
import os
import random
import time

import pytest

import terminalquest as tq
from test_game import FakeScreen, enter, solve
from test_features import StyledScreen
from test_ui import ScriptedScreen, fake_curses, qgame, restore_color_globals  # noqa: F401

OX, OY = 1, 3


# ==========================================================================
# running real commands
# ==========================================================================

@pytest.fixture
def box(tmp_path):
    return str(tmp_path)


def run(box, line):
    return tq.run_command(line, box, box)


class TestCommandExecution:
    def test_binary_output_does_not_crash(self, box):
        # Real bug: output was decoded strictly, so any non-UTF-8 byte raised
        # UnicodeDecodeError out of the game and dumped a traceback over the
        # screen.
        _, out, err = run(box, r"printf '\xff\xfe\x80 bytes'")
        assert "bytes" in out and "�" in out and err == ""

    def test_unicode_round_trips(self, box):
        assert run(box, "echo 'héllo ✓'")[1] == "héllo ✓\n"

    @pytest.mark.parametrize("line", ["cat", "read x; echo got:[$x]", "head -1", "sort", "wc -l"])
    def test_commands_that_read_the_keyboard_get_end_of_input_at_once(self, box, line):
        # Real bug: stdin was the player's own terminal, so a bare `cat` (a
        # very common beginner slip) swallowed their keystrokes and froze the
        # game for the full 10-second timeout.
        t0 = time.monotonic()
        run(box, line)
        assert time.monotonic() - t0 < 3

    def test_a_background_job_neither_blocks_nor_outlives_its_command(self, box):
        t0 = time.monotonic()
        _, out, _ = run(box, "sleep 60 & echo $!")
        assert time.monotonic() - t0 < 3
        pid = int(out.split()[0])
        time.sleep(0.3)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_command_that_runs_too_long_is_stopped_and_cleaned_up(self, box, monkeypatch):
        monkeypatch.setattr(tq, "COMMAND_TIMEOUT", 1)
        t0 = time.monotonic()
        cwd, out, err = run(box, "sleep 30")
        assert 0.9 < time.monotonic() - t0 < 5
        assert cwd == box and out == "" and "too long" in err

    def test_runaway_output_is_cut_off_instead_of_filling_memory(self, box):
        t0 = time.monotonic()
        _, out, _ = run(box, "yes")
        assert time.monotonic() - t0 < 5
        assert len(out) <= tq.OUTPUT_SHOWN + 40 and out.endswith("(output cut off)")

    def test_runaway_error_output_is_cut_off_too(self, box):
        _, _, err = run(box, "yes >&2")
        assert len(err) <= tq.OUTPUT_SHOWN + 40 and err.endswith("(output cut off)")

    def test_a_file_cannot_grow_past_the_cap(self, box):
        run(box, "yes > big.txt")
        assert os.path.getsize(os.path.join(box, "big.txt")) <= tq.OUTPUT_CAP

    def test_normal_output_is_not_truncated(self, box):
        _, out, _ = run(box, "seq 1 2000")
        assert out.splitlines()[-1] == "2000" and "cut off" not in out

    def test_the_working_directory_is_still_tracked(self, box):
        cwd, _, _ = run(box, "mkdir deep && cd deep")
        assert os.path.basename(cwd) == "deep"

    def test_a_failing_command_still_reports_its_error(self, box):
        _, out, err = run(box, "ls no-such-thing")
        assert out == "" and "No such file" in err

    def test_stdout_and_stderr_stay_separate(self, box):
        _, out, err = run(box, "echo fine; ls nope")
        assert out == "fine\n" and "nope" in err


# ==========================================================================
# the renderer
# ==========================================================================

def draw(game, cls=FakeScreen, backdrop=False, size=(40, 160)):
    s = cls(*size)
    tq.draw_base(s, game, backdrop=backdrop)
    return s


class CountingScreen(StyledScreen):
    def __init__(self, *a):
        super().__init__(*a)
        self.calls = 0

    def addstr(self, y, x, text, style=0):
        self.calls += 1
        super().addstr(y, x, text, style)


def style_at(screen, game, x, y):
    return screen.styles[(OY + y, OX + x * tq.CELL_W)]


class TestMapTiles:
    def test_sealed_levels_render_as_fog_not_as_walls(self, qgame):
        dump = draw(qgame).dump()
        assert tq.FOG_GLYPH in dump
        fog = [(x, y) for y in range(tq.GRID_H) for x in range(tq.GRID_W)
               if tq._zone_for(x, y) is not None and tq._zone_for(x, y) > qgame.stage]
        text, style = tq._cell(qgame, *fog[0], backdrop=False)
        assert text == tq.FOG_GLYPH * tq.CELL_W and style & curses.A_DIM
        wall = next((x, y) for y in range(tq.GRID_H) for x in range(tq.GRID_W) if qgame.grid[y][x] == "#"
                    and tq._zone_for(x, y) == 1)
        assert tq._cell(qgame, *wall, backdrop=False)[0] == tq.WALL_GLYPH * tq.CELL_W

    def test_fog_lifts_when_the_level_is_reached(self, qgame):
        qgame.stage = 3
        assert tq.FOG_GLYPH not in draw(qgame).dump()

    def test_walls_and_fog_use_different_styles(self, qgame, monkeypatch):
        monkeypatch.setattr(tq, "CP_WALL", 6 << 8)
        wall = next((x, y) for y in range(tq.GRID_H) for x in range(tq.GRID_W) if qgame.grid[y][x] == "#"
                    and tq._zone_for(x, y) == 1)
        fog = next((x, y) for y in range(tq.GRID_H) for x in range(tq.GRID_W)
                   if tq._zone_for(x, y) == 3)
        assert tq._cell(qgame, *wall, False)[1] != tq._cell(qgame, *fog, False)[1]

    def test_a_solved_terminal_turns_green_and_dim(self, qgame, monkeypatch):
        monkeypatch.setattr(tq, "CP_GOOD", 5 << 8)
        monkeypatch.setattr(tq, "CP_COMP", 4 << 8)
        pos = next(p for p, sid in tq.COMPUTERS.items() if sid == "A")
        before = tq._cell(qgame, *pos, False)[1]
        qgame.stations["A"].solved = True
        after = tq._cell(qgame, *pos, False)[1]
        assert before & (4 << 8) and after & (5 << 8) and after & curses.A_DIM

    def test_unlocked_doors_are_green(self, qgame, monkeypatch):
        monkeypatch.setattr(tq, "CP_GOOD", 5 << 8)
        pos = tq.DOORS["doorAB"]["pos"]
        qgame.doors["doorAB"]["locked"] = False
        assert tq._cell(qgame, *pos, False)[1] & (5 << 8)

    def test_coins_twinkle_over_time(self, qgame):
        pos = next(p for p, alive in qgame.coins.items() if alive and tq._zone_for(*p) == 1)
        styles = set()
        for tick in range(40):
            qgame.tick_count = tick
            styles.add(tq._cell(qgame, *pos, False)[1])
        assert len(styles) == 2          # bright most of the time, a dim twinkle now and then

    def test_the_exit_pulses_only_once_every_station_is_solved(self, qgame):
        qgame.stage = 3
        x, y = tq.EXIT_POS
        seen = set()
        for tick in range(12):
            qgame.tick_count = tick
            seen.add(tq._cell(qgame, x, y, False)[1])
        assert len(seen) == 1
        for sid in tq.STATION_ORDER:
            qgame.stations[sid].solved = True
        seen = set()
        for tick in range(12):
            qgame.tick_count = tick
            seen.add(tq._cell(qgame, x, y, False)[1])
        assert len(seen) == 2 and any(s & curses.A_REVERSE for s in seen)

    def test_a_backdrop_is_dimmed(self, qgame):
        for pos in ((2, 2), tq.EXIT_POS):
            assert tq._cell(qgame, *pos, backdrop=True)[1] & curses.A_DIM


class TestLookupTables:
    """The renderer swapped two per-tile searches for precomputed lookups; these
    pin them to the original, obviously-correct logic."""

    def test_the_door_index_matches_scanning_every_door(self, qgame):
        for y in range(tq.GRID_H):
            for x in range(tq.GRID_W):
                scanned = next((d for d in qgame.doors.values() if d["pos"] == (x, y)), None)
                assert qgame.door_at(x, y) is scanned

    def test_the_door_index_shares_state_with_the_doors(self, qgame):
        door = qgame.door_at(*tq.DOORS["doorAB"]["pos"])
        qgame.doors["doorAB"]["locked"] = False
        assert door["locked"] is False and qgame.door_at(*tq.DOORS["doorAB"]["pos"])["locked"] is False

    def test_the_zone_map_matches_computing_each_zone(self):
        for y in range(tq.GRID_H):
            for x in range(tq.GRID_W):
                assert tq.ZONE_MAP[y][x] == tq._zone_for(x, y)

    def test_the_wall_fast_path_matches_the_full_classifier(self, qgame):
        qgame.stage = 3
        for y in range(tq.GRID_H):
            for x in range(tq.GRID_W):
                glyph, kind = tq.tile_glyph(qgame, x, y)
                text, style = tq._cell(qgame, x, y, backdrop=False)
                if kind == "wall":
                    assert text == tq.WALL_GLYPH * tq.CELL_W and style == tq._TILE_STYLE_KEYS["wall"]()


class TestBatchedDrawing:
    def test_batching_paints_exactly_what_tile_by_tile_drawing_would(self, qgame):
        # A differential test: the optimized renderer must be pixel-identical
        # to the obvious one.
        qgame.stage = 2
        for sid in "AB":
            qgame.stations[sid].solved = True
        qgame.tick_count = 7
        for backdrop in (False, True):
            batched = StyledScreen(40, 160)
            batched.erase()
            tq._draw_map(batched, qgame, OX, OY, backdrop)
            reference = StyledScreen(40, 160)
            for y in range(tq.GRID_H):
                for x in range(tq.GRID_W):
                    text, style = tq._cell(qgame, x, y, backdrop)
                    reference.addstr(OY + y, OX + x * tq.CELL_W, text, style)
            assert batched.dump() == reference.dump()
            assert batched.styles == reference.styles

    def test_a_frame_makes_far_fewer_writes_than_there_are_tiles(self, qgame):
        s = CountingScreen(40, 160)
        tq.draw_base(s, qgame)
        assert s.calls < tq.GRID_W * tq.GRID_H / 3

    def test_a_frame_is_cheap(self, qgame):
        # A generous budget (tick is 100 ms); this guards against an
        # accidental O(n^2) or per-frame disk/process work sneaking in.
        s = FakeScreen(40, 160)
        qgame.stage = 3
        t0 = time.perf_counter()
        for _ in range(200):
            tq.draw_base(s, qgame)
        assert (time.perf_counter() - t0) / 200 < 0.02

    def test_a_write_that_does_not_fit_is_dropped_not_raised(self, qgame):
        tq.draw_base(FakeScreen(28, 112), qgame)      # the smallest allowed window
        tq._put(FakeScreen(5, 5), 4, 3, "far too long for this", 0)   # no exception


class TestHud:
    def _dump(self, game, **kw):
        return draw(game, **kw).dump()

    def test_hearts_show_lives_left_and_lives_lost(self, qgame):
        qgame.lives = 2
        assert "♥♥♡" in self._dump(qgame)
        qgame.lives = 0
        assert "♡♡♡" in self._dump(qgame)

    def test_the_progress_bar_tracks_solved_stations(self, qgame):
        assert "▱" * 8 + " 0/8 stations" in self._dump(qgame)
        for sid in "ABC":
            qgame.stations[sid].solved = True
        assert "▰▰▰▱▱▱▱▱ 3/8 stations" in self._dump(qgame)

    def test_the_level_tag_follows_the_stage(self, qgame):
        assert "LEVEL 1/3" in self._dump(qgame)
        qgame.stage = 3
        assert "LEVEL 3/3" in self._dump(qgame)

    def test_the_legend_names_every_kind_of_tile(self):
        assert [t for t, _ in tq.LEGEND_TOKENS] == [
            "[▶] you", "x enemy", "▓ locked door", "▣A terminal", "* coin", "X exit"]

    def test_every_legend_token_is_drawn_in_its_own_style(self, qgame, monkeypatch):
        monkeypatch.setattr(tq, "CP_ENEMY", 2 << 8)
        monkeypatch.setattr(tq, "CP_GOOD", 5 << 8)
        s = draw(qgame, StyledScreen)
        row = OY + tq.GRID_H + 2
        line = "".join(s.rows[row])
        assert "    ".join(t for t, _ in tq.LEGEND_TOKENS) in line
        assert s.styles[(row, line.index("x enemy"))] & (2 << 8)
        assert s.styles[(row, line.index("X exit"))] & (5 << 8)

    def test_a_backdrop_has_no_hud_and_no_sealed_banners(self, qgame):
        dump = self._dump(qgame, backdrop=True)
        for text in ("TERMINALQUEST", "stations", "[q] quit", "SEALED", "so far"):
            assert text not in dump

    @pytest.mark.parametrize("draw_fn, setup", [
        (tq.draw_sign, lambda g: setattr(g, "sign_text", "hello")),
        (tq.draw_quitconfirm, lambda g: None),
        (tq.draw_terminal, lambda g: g.enter_terminal("A")),
    ])
    def test_popups_do_not_leak_the_hud_around_the_box(self, qgame, draw_fn, setup):
        setup(qgame)
        s = FakeScreen(40, 160)
        draw_fn(s, qgame)
        dump = s.dump()
        assert "[q] quit" not in dump.replace("QUIT TERMINALQUEST?", "") and "next: terminal" not in dump

    def test_the_hud_never_overruns_a_narrow_window(self, qgame):
        qgame.learned.extend(["ls", "cat", "cd", "mkdir", "touch", "cp", "mv", "rm"])
        for width in (112, 90, 60, 30):
            tq.draw_base(FakeScreen(40, width), qgame)   # FakeScreen raises if a write passes the edge


class TestPopups:
    def test_a_popup_is_framed_padded_and_opaque(self, qgame):
        s = FakeScreen(40, 160)
        tq.draw_quitconfirm(s, qgame)
        lines = [l for l in s.dump().splitlines() if "┌" in l or "│" in l or "└" in l]
        assert lines[0].count("┌") == 1 and lines[-1].count("└") == 1
        inner = [l for l in lines if "│" in l]
        assert all(l.count("│") == 2 for l in inner)               # both borders, nothing behind the box
        between = [l[l.index("│") + 1:l.rindex("│")].strip() for l in inner]
        assert between[0] == "" and between[-1] == ""              # a padding row top and bottom

    def test_long_text_wraps_inside_the_box(self, qgame):
        qgame.mode, qgame.sign_text = "sign", "word " * 60
        s = FakeScreen(40, 160)
        tq.draw_sign(s, qgame)
        box = [l for l in s.dump().splitlines() if "│" in l]
        assert len(box) > 5
        left = box[0].index("│")
        assert all(l.index("│") == left for l in box)              # a straight edge

    def test_a_reversed_row_spans_the_full_box_width(self, qgame):
        s = StyledScreen(40, 160)
        tq.draw_quitconfirm(s, qgame)
        y = next(i for i, row in enumerate(s.rows) if "Y = quit" in "".join(row))
        left = "".join(s.rows[y]).index("│")
        right = "".join(s.rows[y]).rindex("│")
        assert all(s.styles[(y, x)] & curses.A_REVERSE for x in range(left + 1, right))

    def test_popups_fit_the_smallest_window(self, qgame):
        qgame.begin_quiz_offer(1, "overworld")
        qgame.quiz_start()
        for fn in (tq.draw_quitconfirm, tq.draw_quizoffer, tq.draw_quiz):
            qgame.mode = "quizoffer" if fn is tq.draw_quizoffer else "quiz"
            fn(FakeScreen(28, 112), qgame) if fn is not tq.draw_quitconfirm else fn(FakeScreen(28, 112), qgame)


class TestBanner:
    def test_the_title_spells_out_in_three_rows(self):
        rows = tq.render_banner("TERMINALQUEST")
        assert len(rows) == 3 and max(map(len, rows)) <= 80      # fits MIN_W with room to spare

    def test_it_uses_only_half_block_characters(self):
        text = "".join(tq.render_banner("TERMINALQUEST") + tq.render_banner("YOU WIN"))
        assert set(text) <= set(" █▀▄")

    def test_every_letter_the_game_spells_exists_in_the_font(self):
        for ch in set("TERMINALQUEST" + "YOU WIN"):
            assert ch in tq._BANNER_FONT, ch

    def test_every_glyph_is_five_rows_of_one_width(self):
        for ch, glyph in tq._BANNER_FONT.items():
            assert len(glyph) == 5 and len({len(r) for r in glyph}) == 1, ch

    def test_an_unknown_character_is_a_gap_not_a_crash(self):
        assert tq.render_banner("A~A")

    def test_distinct_letters_look_different(self):
        shapes = {ch: tuple(tq.render_banner(ch)) for ch in "TERMINALQUS"}
        assert len(set(shapes.values())) == len(shapes)


class TestScreens:
    def test_the_intro_fits_the_smallest_window_and_shows_everything(self, qgame):
        s = FakeScreen(28, 112)
        tq.draw_intro(s, qgame)
        dump = s.dump()
        for text in ("press any key to start", "arrow keys move", "the way out", "signal lost"):
            assert text in dump
        assert any(c in dump for c in "█▀▄")

    def test_game_over_reports_how_far_you_got(self, qgame):
        qgame.stations["A"].solved = qgame.stations["B"].solved = True
        qgame.score = 4
        s = FakeScreen(30, 100)
        tq.draw_gameover(s, qgame)
        assert "GAME OVER" in s.dump() and "stations cleared 2/8" in s.dump() and "coins *4" in s.dump()
        assert "press any key to try again, or q to quit" in s.dump()

    def test_the_solved_panel_shows_what_you_typed_and_what_bash_said(self, qgame):
        solve(qgame, "B")
        qgame.enter_terminal("B")
        s = FakeScreen(40, 160)
        tq.draw_terminal(s, qgame)
        dump = s.dump()
        assert "SOLVED" in dump and "heading back to the ship" in dump
        assert "$ cat key.txt" in dump and "something unlocks" in dump

    def test_a_station_marked_solved_without_an_explanation_still_draws(self, qgame):
        # the explanation is normally filled in at solve time; a station flagged
        # solved some other way (a loaded save, a test) must not crash the panel
        qgame.stations["A"].solved = True
        qgame.enter_terminal("A")
        tq.draw_terminal(FakeScreen(40, 160), qgame)

    def test_lives_start_at_the_constant(self, qgame):
        assert qgame.lives == tq.MAX_LIVES
        qgame.lives = 0
        qgame.retry()
        assert qgame.lives == tq.MAX_LIVES


# ==========================================================================
# the fuzzer
# ==========================================================================

SAFE_LINES = ["ls", "cat key.txt", "cd vault", "mkdir stash", "touch spare.key", "cp template.txt backup.txt",
              "mv draft.txt final.txt", "rm jam.lock", "cat", "cat nosuch", "ls ..", "echo hi", "pwd", "",
              "héllo ✓", "x" * 90, "cd ..", "rm -rf ~", "sudo ls", "echo $HOME", "ls /etc"]
MOVES = [curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT]
SIZES = [(28, 112), (30, 120), (40, 160), (24, 80), (10, 40), (28, 111), (27, 112), (60, 200), (3, 3)]


def random_script(rng, length):
    keys = []
    while len(keys) < length:
        r = rng.random()
        if r < 0.45:
            keys.append(rng.choice(MOVES))
        elif r < 0.55:
            keys.append(rng.choice(["q", "Q", "e", "b", "t", " ", "y", "n", "?", "\t", 27]))
        elif r < 0.62:
            keys.extend(rng.choice(SAFE_LINES))
            keys.append(rng.choice([10, 13, curses.KEY_ENTER]))
        elif r < 0.70:
            keys.append(rng.choice([-1, -1, -1, curses.KEY_RESIZE, curses.KEY_BACKSPACE, 127, 8]))
        elif r < 0.78:
            keys.append(rng.randrange(0, 600))
        elif r < 0.85:
            keys.append(rng.choice("1234"))
        else:
            keys.append(rng.choice(MOVES))
    return keys


def fuzz_once(seed, tmp_path, monkeypatch, length=160):
    rng = random.Random(seed)
    game = tq.Game(str(tmp_path / f"fz{seed}"), reset=True, rng=random.Random(seed))
    # make most seeds reach the terminal / quiz / win modes, not just wander the map
    if seed % 3 == 0:
        game.enter_terminal(rng.choice(tq.STATION_ORDER[:2]))
    elif seed % 3 == 1:
        game.begin_quiz_offer(1, "overworld")
    monkeypatch.setattr(tq, "Game", lambda *a, **k: game)
    h, w = rng.choice(SIZES)
    screen = ScriptedScreen([" "] + random_script(rng, length), h, w)
    try:
        tq.main(screen)
    except AssertionError as e:
        assert "kept running after the script ended" in str(e), e    # the script ran dry: that's the normal ending
    return game


def play(game, keys, monkeypatch, size=(40, 160)):
    monkeypatch.setattr(tq, "Game", lambda *a, **k: game)
    screen = ScriptedScreen(keys, *size)
    try:
        tq.main(screen)
    except AssertionError as e:
        assert "kept running" in str(e)
    return screen


class TestWindowResize:
    """A resize arrives as the pseudo-key KEY_RESIZE. It used to count as 'any
    key': it restarted the game from the game-over screen, dismissed signs, and
    started the game unread from the intro."""
    R = curses.KEY_RESIZE

    def test_resizing_on_the_game_over_screen_does_not_restart(self, qgame, fake_curses, monkeypatch):
        qgame.mode = "gameover"
        qgame.lives = 0
        play(qgame, [" ", self.R, self.R], monkeypatch)
        assert qgame.mode == "gameover" and qgame.lives == 0

    def test_resizing_does_not_dismiss_a_sign(self, qgame, fake_curses, monkeypatch):
        qgame.mode, qgame.sign_text = "sign", "hello"
        play(qgame, [" ", self.R], monkeypatch)
        assert qgame.mode == "sign"

    def test_resizing_does_not_leave_a_solved_terminal(self, qgame, fake_curses, monkeypatch):
        solve(qgame, "A")
        qgame.enter_terminal("A")
        play(qgame, [" ", self.R], monkeypatch)
        assert qgame.mode == "terminal"

    def test_resizing_does_not_dismiss_the_level_screen(self, qgame, fake_curses, monkeypatch):
        qgame.mode, qgame.congrats_text, qgame.congrats_level = "congrats", "LEVEL 1 COMPLETE!", 1
        play(qgame, [" ", self.R, self.R], monkeypatch)
        assert qgame.mode == "congrats"

    def test_resizing_on_the_intro_does_not_start_the_game(self, qgame, fake_curses, monkeypatch):
        screen = play(qgame, [self.R, self.R, "x", "q", "y"], monkeypatch)
        assert qgame.mode == "quitconfirm"        # only the real key "x" started it
        assert screen.cleared == 2                 # and each resize forced a repaint

    def test_a_resize_forces_a_full_repaint(self, qgame, fake_curses, monkeypatch):
        screen = play(qgame, [" ", self.R, self.R, self.R], monkeypatch)
        assert screen.cleared == 3

    def test_resizing_below_the_minimum_then_back_recovers(self, qgame, fake_curses, monkeypatch):
        screen = play(qgame, [" ", self.R, "x", self.R, "q"], monkeypatch, size=(10, 40))
        assert "resize" in screen.dump().lower()

    def test_resizing_on_the_win_screen_repaints_without_quitting(self, qgame):
        qgame.stage = 4
        keys = iter([self.R, ord(":"), self.R, ord("w"), ord("q"), 10])

        class Scr(FakeScreen):
            def getch(self):
                return next(keys)

        scr = Scr(40, 160)
        tq.wait_for_quit(scr, qgame)
        assert scr.cleared == 2


class TestFuzz:
    @pytest.mark.parametrize("seed", range(10))
    def test_random_play_never_crashes(self, seed, tmp_path, monkeypatch, fake_curses):
        game = fuzz_once(seed, tmp_path, monkeypatch)
        # invariants that must hold however the player behaved
        assert 0 <= game.lives <= tq.MAX_LIVES
        assert 0 <= game.px < tq.GRID_W and 0 <= game.py < tq.GRID_H
        assert game.grid[game.py][game.px] != "#"
        assert game.mode in tq.DRAWERS or game.mode == "win"
        assert len(game.input_buf) <= 60

    def test_a_long_run_stays_consistent(self, tmp_path, monkeypatch, fake_curses):
        game = fuzz_once(1234, tmp_path, monkeypatch, length=500)
        assert 0 <= game.lives <= tq.MAX_LIVES

    def test_no_screen_ever_raises_at_any_window_size(self, qgame):
        # every renderer must swallow "doesn't fit" itself — a window being
        # dragged smaller mid-game must never surface as a traceback
        qgame.begin_quiz_offer(1, "overworld")
        for h, w in SIZES * 2:
            for fn in (tq.draw_overworld, tq.draw_intro, tq.draw_gameover, tq.draw_quizoffer, tq.draw_quitconfirm):
                fn(FakeScreen(h, w), qgame)
            tq.draw_too_small(FakeScreen(h, w))
