# Contributing to TerminalQuest

Issues and PRs are welcome. This is a small, opinionated project on
purpose — read the [design rules](README.md#design-rules-read-this-before-opening-a-pr)
in the README before writing code; most rejected PRs are rejected for
growing the scope, not for code quality.

## Before you start

- **Open an issue first for anything non-trivial** (a new feature, a
  behavior change, a new command). Bug fixes and small polish don't need
  one — just open the PR.
- `main` is protected: nothing merges without a pull request, and only
  the maintainer merges. Push access isn't required to contribute —
  everything below works from a fork.

## Workflow

1. **Fork** the repo (button top-right on GitHub), then clone your fork:
   ```bash
   git clone https://github.com/<you>/TerminalQuest.git
   cd TerminalQuest
   ```
2. **Branch off `main`** with a short, descriptive name:
   ```bash
   git checkout -b fix/coin-count-off-by-one
   ```
3. **Make your change.** No dependencies beyond the Python standard
   library for the game itself (`requirements-dev.txt` is for the test
   suite only).
4. **Add or update a test** in `tests/` for any behavior change —
   `test_game.py` for core game logic, `test_quiz.py` for the quiz,
   `test_ui.py` / `test_features.py` for screens and newer features.
5. **Run the checks locally, both should pass clean:**
   ```bash
   pip install -r requirements-dev.txt   # first time only
   pytest
   python3 terminalquest.py --selftest
   ```
6. **Push your branch and open a PR** against `sarangrakhecha/TerminalQuest:main`:
   ```bash
   git push origin fix/coin-count-off-by-one
   ```
   Describe what changed and why in the PR description. Keep diffs small —
   a 20-line fix beats a 200-line diff that also refactors three unrelated
   things.
7. CI runs automatically on the PR. The maintainer reviews and merges —
   nothing lands on `main` without that review.

## What's genuinely welcome

More rooms, enemies, or secrets in the existing map; sound-free visual
polish; bug fixes; clearer error messages; test coverage.

## What's out of scope

New commands beyond the fixed 8, scoring/achievement systems, external
runtime dependencies, or growing past three small levels. See the
[design rules](README.md#design-rules-read-this-before-opening-a-pr) for
the reasoning — if you want the bigger version, forking into your own
project is genuinely the right move, not a rejection of the idea.

## Reporting a bug

See [Reporting a bug](README.md#reporting-a-bug) in the README for what
to include.
