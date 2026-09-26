"""Tests for the optional end-of-level quiz."""
import random

import pytest

import terminalquest as tq
from test_game import solve  # noqa: E402  (tests/ is on sys.path under pytest)

ALL_QUESTIONS = [(lvl, q) for lvl, pool in tq.QUIZ_POOLS.items() for q in pool]
TYPED = [(lvl, q) for lvl, q in ALL_QUESTIONS if q["kind"] == "type"]
CHOICE = [(lvl, q) for lvl, q in ALL_QUESTIONS if q["kind"] == "choice"]


def _id(pair):
    return f"L{pair[0]}:{pair[1]['prompt'][:40]}"


@pytest.fixture
def quiz_game(tmp_path):
    root = tmp_path / "tq"
    root.mkdir()
    return tq.Game(str(root), reset=True, quiz=True, rng=random.Random(7))


def offer(game, level=1, then="overworld"):
    game.begin_quiz_offer(level, then)
    game.quiz_start()
    assert game.mode == "quiz"


def right_answer(game):
    """Submit the correct answer to the current question."""
    q = game.quiz_question()
    if q["kind"] == "type":
        game.input_buf = q["answer"]
        game.quiz_submit()
    else:
        game.quiz_submit(q["correct"])


def wrong_answer(game):
    q = game.quiz_question()
    if q["kind"] == "type":
        game.input_buf = "echo definitely-not-it"
        game.quiz_submit()
    else:
        game.quiz_submit((q["correct"] + 1) % len(q["options"]))


# ---- the question pools themselves ---------------------------------------

class TestQuestionPools:
    def test_every_level_has_a_pool_bigger_than_a_quiz(self):
        for level in (1, 2, 3):
            assert len(tq.QUIZ_POOLS[level]) > tq.QUIZ_LEN

    def test_no_duplicate_prompts_within_a_level(self):
        for pool in tq.QUIZ_POOLS.values():
            prompts = [q["prompt"] for q in pool]
            assert len(prompts) == len(set(prompts))

    @pytest.mark.parametrize("pair", ALL_QUESTIONS, ids=_id)
    def test_every_question_has_a_hint_and_an_answer(self, pair):
        _, q = pair
        assert q["hint"].strip() and q["answer"].strip() and q["prompt"].strip()

    @pytest.mark.parametrize("pair", CHOICE, ids=_id)
    def test_choice_questions_have_one_valid_correct_option(self, pair):
        _, q = pair
        assert 0 <= q["correct"] < len(q["options"])
        assert len(set(q["options"])) == len(q["options"])
        assert q["why"].strip()

    @pytest.mark.parametrize("pair", TYPED, ids=_id)
    def test_the_stated_answer_actually_passes_real_bash_grading(self, pair):
        _, q = pair
        ok, _problem = tq.grade_typed(q, q["answer"])
        assert ok, f"canonical answer {q['answer']!r} fails its own check"

    @pytest.mark.parametrize("pair", TYPED, ids=_id)
    def test_a_do_nothing_command_never_passes(self, pair):
        _, q = pair
        for line in ("true", "pwd", "echo hi"):
            ok, _ = tq.grade_typed(q, line)
            assert not ok, f"{line!r} passed {q['prompt']!r}"

    def test_grading_reports_a_real_bash_error(self):
        q = next(q for _, q in TYPED if q["answer"] == "cat notes.txt")
        ok, problem = tq.grade_typed(q, "cat nosuchfile.txt")
        assert not ok and "No such file" in problem

    def test_grading_accepts_any_command_that_gets_the_right_result(self):
        q = next(q for _, q in TYPED if q["answer"] == "mkdir photos")
        assert tq.grade_typed(q, "mkdir -p photos")[0]
        assert not tq.grade_typed(q, "mkdir pictures")[0]


# ---- drawing three ---------------------------------------------------------

class TestDraw:
    def test_a_quiz_has_three_distinct_questions_from_that_levels_pool(self, quiz_game):
        for level in (1, 2, 3):
            offer(quiz_game, level)
            prompts = [i["prompt"] for i in quiz_game.quiz["items"]]
            assert len(prompts) == tq.QUIZ_LEN == len(set(prompts))
            assert set(prompts) <= {q["prompt"] for q in tq.QUIZ_POOLS[level]}

    def test_same_seed_gives_the_same_quiz(self, tmp_path):
        def draw(seed):
            g = tq.Game(str(tmp_path / f"s{seed}"), reset=True, rng=random.Random(seed))
            g.begin_quiz_offer(1, "overworld")
            g.quiz_start()
            return [i["prompt"] for i in g.quiz["items"]]
        assert draw(1) == draw(1)

    def test_different_seeds_produce_different_quizzes(self, tmp_path):
        seen = set()
        for seed in range(12):
            g = tq.Game(str(tmp_path / f"d{seed}"), reset=True, rng=random.Random(seed))
            g.begin_quiz_offer(1, "overworld")
            g.quiz_start()
            seen.add(tuple(i["prompt"] for i in g.quiz["items"]))
        assert len(seen) > 1

    def test_choice_options_are_shuffled_but_the_right_one_is_tracked(self, tmp_path):
        positions = set()
        for seed in range(30):
            g = tq.Game(str(tmp_path / f"c{seed}"), reset=True, rng=random.Random(seed))
            g.begin_quiz_offer(1, "overworld")
            g.quiz_start()
            for item in g.quiz["items"]:
                if item["kind"] == "choice":
                    original = next(q for q in tq.QUIZ_POOLS[1] if q["prompt"] == item["prompt"])
                    assert item["options"][item["correct"]] == original["answer"]
                    positions.add(item["correct"])
        assert len(positions) > 1


# ---- the flow -----------------------------------------------------------------

class TestFlow:
    def test_the_quiz_is_optional_skipping_goes_straight_on(self, quiz_game):
        quiz_game.begin_quiz_offer(1, "overworld")
        assert quiz_game.mode == "quizoffer"
        quiz_game.quiz_skip()
        assert quiz_game.mode == "overworld"
        assert quiz_game.quiz_taken == 0

    def test_skipping_after_the_last_level_still_wins(self, quiz_game):
        quiz_game.begin_quiz_offer(3, "win")
        quiz_game.quiz_skip()
        assert quiz_game.mode == "win"
        assert quiz_game.stage == len(tq.STAGES) + 1

    def test_all_right_answers_finish_with_three_of_three(self, quiz_game):
        offer(quiz_game)
        for _ in range(tq.QUIZ_LEN):
            right_answer(quiz_game)
            assert quiz_game.quiz["phase"] == "feedback"
            quiz_game.quiz_continue()
        assert quiz_game.mode == "quizdone"
        assert quiz_game.quiz["right"] == tq.QUIZ_LEN
        quiz_game.quiz_finish()
        assert quiz_game.mode == "overworld"
        assert (quiz_game.quiz_right, quiz_game.quiz_taken) == (3, 3)

    def test_first_wrong_answer_gives_a_hint_and_lets_you_retry(self, quiz_game):
        offer(quiz_game)
        wrong_answer(quiz_game)
        z = quiz_game.quiz
        assert z["phase"] == "asking" and z["attempts"] == 1
        assert any("Hint" in ln for ln in z["lines"])
        assert z["idx"] == 0
        right_answer(quiz_game)
        assert quiz_game.quiz["phase"] == "feedback" and quiz_game.quiz["right"] == 1

    def test_second_wrong_answer_reveals_the_answer_and_moves_on(self, quiz_game):
        offer(quiz_game)
        q = quiz_game.quiz_question()
        wrong_answer(quiz_game)
        wrong_answer(quiz_game)
        z = quiz_game.quiz
        assert z["phase"] == "feedback"
        assert any(q["answer"] in ln for ln in z["lines"])
        assert z["right"] == 0
        quiz_game.quiz_continue()
        assert z["idx"] == 1 and z["attempts"] == 0 and z["phase"] == "asking"

    def test_a_typed_wrong_answer_shows_the_real_bash_error(self, quiz_game):
        offer(quiz_game)
        quiz_game.quiz["items"][0] = dict(next(q for _, q in TYPED if q["answer"] == "cat notes.txt"))
        quiz_game.input_buf = "cat nosuchfile.txt"
        quiz_game.quiz_submit()
        assert any("No such file" in ln for ln in quiz_game.quiz["lines"])

    def test_quizzes_never_cost_lives_or_score(self, quiz_game):
        lives, score = quiz_game.lives, quiz_game.score
        offer(quiz_game)
        for _ in range(tq.QUIZ_LEN):
            wrong_answer(quiz_game)
            wrong_answer(quiz_game)
            quiz_game.quiz_continue()
        assert (quiz_game.lives, quiz_game.score) == (lives, score)
        assert quiz_game.quiz_right == 0 and quiz_game.quiz_taken == 3

    def test_escape_mid_quiz_bails_out_cleanly(self, quiz_game):
        offer(quiz_game)
        right_answer(quiz_game)
        quiz_game.quiz_skip()
        assert quiz_game.mode == "overworld" and quiz_game.quiz is None

    def test_empty_typed_answer_is_ignored_not_counted_wrong(self, quiz_game):
        offer(quiz_game)
        q = quiz_game.quiz["items"][0] = dict(next(q for _, q in TYPED))
        quiz_game.input_buf = "   "
        quiz_game.quiz_submit()
        assert quiz_game.quiz["attempts"] == 0

    def test_typing_is_ignored_while_reading_feedback(self, quiz_game):
        offer(quiz_game)
        right_answer(quiz_game)
        quiz_game.quiz_char("x")
        assert quiz_game.input_buf == ""


# ---- wired into the game ----------------------------------------------------------

class TestIntegration:
    def test_a_level_complete_screen_leads_to_the_quiz_offer(self, quiz_game):
        quiz_game.mode, quiz_game.congrats_level = "congrats", 1
        quiz_game.dismiss_congrats()
        assert quiz_game.mode == "quizoffer" and quiz_game.quiz["level"] == 1
        quiz_game.quiz_skip()
        assert quiz_game.mode == "overworld"

    def test_quiz_can_be_switched_off(self, tmp_path):
        g = tq.Game(str(tmp_path / "off"), reset=True, quiz=False)
        g.mode, g.congrats_level = "congrats", 1
        g.dismiss_congrats()
        assert g.mode == "overworld"

    def test_reaching_the_exit_offers_the_level_3_quiz_then_wins(self, tmp_path):
        g = tq.Game(str(tmp_path / "e2e"), reset=True, quiz=False, rng=random.Random(3))
        for sid in tq.STATION_ORDER:
            solve(g, sid)
        tq.collect_all_coins(g)
        g.quiz_enabled = True
        tq.walk_to(g, tq.EXIT_POS)
        assert g.mode == "quizoffer" and g.quiz["level"] == 3
        g.quiz_start()
        for _ in range(tq.QUIZ_LEN):
            right_answer(g)
            g.quiz_continue()
        g.quiz_finish()
        assert g.mode == "win"
        assert (g.quiz_right, g.quiz_taken) == (3, 3)

    def test_win_screen_mentions_the_quiz_score_only_if_taken(self, tmp_path):
        from test_game import FakeScreen
        g = tq.Game(str(tmp_path / "w"), reset=True, quiz=False)
        g.mode = "win"
        s = FakeScreen(); tq.draw_win(s, g)
        assert "quiz:" not in s.dump()
        g.quiz_taken, g.quiz_right = 3, 2
        s = FakeScreen(); tq.draw_win(s, g)
        assert "quiz: 2/3" in s.dump()


# ---- rendering -------------------------------------------------------------------------

class TestRendering:
    @pytest.mark.parametrize("size", [(40, 160), (30, 112), (12, 40)])
    def test_every_quiz_screen_draws_without_raising(self, quiz_game, size):
        from test_game import FakeScreen
        quiz_game.begin_quiz_offer(1, "overworld")
        s = FakeScreen(*size); tq.draw_quizoffer(s, quiz_game)
        quiz_game.quiz_start()
        for _ in range(tq.QUIZ_LEN):
            s = FakeScreen(*size); tq.draw_quiz(s, quiz_game)
            wrong_answer(quiz_game)
            s = FakeScreen(*size); tq.draw_quiz(s, quiz_game)
            wrong_answer(quiz_game)
            s = FakeScreen(*size); tq.draw_quiz(s, quiz_game)
            quiz_game.quiz_continue()
        s = FakeScreen(*size); tq.draw_quizdone(s, quiz_game)

    def test_offer_screen_says_it_is_optional(self, quiz_game):
        from test_game import FakeScreen
        quiz_game.begin_quiz_offer(2, "overworld")
        s = FakeScreen(); tq.draw_quizoffer(s, quiz_game)
        dump = s.dump()
        assert "optional" in dump.lower() and "skip" in dump.lower()

    def test_a_choice_question_shows_numbered_options(self, quiz_game):
        from test_game import FakeScreen
        offer(quiz_game)
        quiz_game.quiz["items"][0] = tq.QUIZ_POOLS[1][4]  # a choice question
        s = FakeScreen(); tq.draw_quiz(s, quiz_game)
        assert "1)" in s.dump() and "4)" in s.dump()


# ---- key handling (the part of the curses loop that used to be untestable) ----

class TestKeys:
    @staticmethod
    def press(game, text):
        for ch in text:
            tq.handle_quiz_key(game, ord(ch) if isinstance(ch, str) else ch)

    def test_y_starts_and_n_skips(self, quiz_game):
        quiz_game.begin_quiz_offer(1, "overworld")
        self.press(quiz_game, "y")
        assert quiz_game.mode == "quiz"
        quiz_game.begin_quiz_offer(1, "overworld")
        self.press(quiz_game, "n")
        assert quiz_game.mode == "overworld"

    def test_space_or_arrow_keys_do_not_accidentally_answer_the_offer(self, quiz_game):
        quiz_game.begin_quiz_offer(1, "overworld")
        self.press(quiz_game, " ")
        tq.handle_quiz_key(quiz_game, 259)  # an arrow key code
        assert quiz_game.mode == "quizoffer"

    def test_escape_skips_from_the_offer_and_mid_quiz(self, quiz_game):
        quiz_game.begin_quiz_offer(1, "overworld")
        self.press(quiz_game, [27])
        assert quiz_game.mode == "overworld"
        offer(quiz_game)
        self.press(quiz_game, [27])
        assert quiz_game.mode == "overworld"

    def test_a_whole_quiz_by_keystrokes(self, quiz_game):
        quiz_game.begin_quiz_offer(1, "overworld")
        self.press(quiz_game, "y")
        for _ in range(tq.QUIZ_LEN):
            q = quiz_game.quiz_question()
            if q["kind"] == "type":
                self.press(quiz_game, q["answer"])
                self.press(quiz_game, [10])
            else:
                self.press(quiz_game, str(q["correct"] + 1))
            assert quiz_game.quiz["phase"] == "feedback"
            self.press(quiz_game, " ")
        assert quiz_game.mode == "quizdone"
        self.press(quiz_game, " ")
        assert quiz_game.mode == "overworld"
        assert quiz_game.quiz_right == 3

    def test_backspace_edits_a_typed_answer(self, quiz_game):
        offer(quiz_game)
        quiz_game.quiz["items"][0] = dict(next(q for _, q in TYPED if q["answer"] == "mkdir photos"))
        self.press(quiz_game, "mkdir photox")
        self.press(quiz_game, [127])
        self.press(quiz_game, "s")
        self.press(quiz_game, [10])
        assert quiz_game.quiz["phase"] == "feedback" and quiz_game.quiz["right"] == 1

    def test_number_keys_beyond_the_options_are_ignored(self, quiz_game):
        offer(quiz_game)
        quiz_game.quiz["items"][0] = tq.QUIZ_POOLS[1][4]
        self.press(quiz_game, "9")
        assert quiz_game.quiz["attempts"] == 0

    def test_letters_do_nothing_on_a_choice_question(self, quiz_game):
        offer(quiz_game)
        quiz_game.quiz["items"][0] = tq.QUIZ_POOLS[1][4]
        self.press(quiz_game, "abc")
        assert quiz_game.quiz["attempts"] == 0 and quiz_game.input_buf == ""

    def test_keys_are_ignored_while_reading_feedback_except_space(self, quiz_game):
        offer(quiz_game)
        right_answer(quiz_game)
        self.press(quiz_game, "abc\n")
        assert quiz_game.quiz["phase"] == "feedback"
        self.press(quiz_game, " ")
        assert quiz_game.quiz["idx"] == 1
