# TerminalQuest — Signal Lost

**Learn the terminal by saving a ship — not by reading slides.**

A tiny real-time arcade game that teaches you the terminal by making you
actually use it.

No slides, no lectures. You move around a ship with arrow keys,
dodge patrolling enemies, and every so often walk up to an old computer.
It drops you into a **real bash prompt**. Whatever you type actually runs,
starting in a scratch folder on your machine. Get it right, and something
in the world reacts — a door unlocks, a gate opens, a locked room stops
being locked. After each level there's an optional 3-question recap quiz to
help it stick — take it or skip it.

**8 commands · 8 stations · 3 levels · 210 automated tests · zero dependencies**

## Contents

- [Why this exists](#why-this-exists)
- [How the game works](#how-the-game-works)
- [What it looks like](#what-it-looks-like)
- [The map](#the-map)
- [Levels, gates, and masking](#levels-gates-and-masking)
- [Install & run](#install--run)
- [Controls](#controls)
- [How it's built](#how-its-built)
- [Design philosophy](#design-philosophy)
- [Safety boundary](#safety-boundary)
- [Project layout](#project-layout)
- [Testing](#testing) — what's actually covered
- [Design rules for contributors](#design-rules-read-this-before-opening-a-pr)
- [Reporting a bug](#reporting-a-bug)
- [Contributing](#contributing)
- [License](#license)

## Why this exists

School (and every bootcamp, tutorial, and AI assistant since) is great at
teaching you *how to code*. Almost nobody teaches you the terminal itself —
`ls`, `cd`, `cat`, `mkdir`, `mv`, `rm` — the stuff you type dozens of times
a day. Most people pick it up the slow way: one Googled error message at a
time, over months.

AI has made looking things up faster. It hasn't made knowing the basics
by heart any less useful — that's still the difference between moving
through your work and constantly stopping to look something up.

TerminalQuest is a small attempt at fixing that the fun way: instead of a
tutorial, a game where the "puzzle" at every station is solved by typing one
real command, in a real shell, with real consequences (a real file gets
created, moved, or deleted) — not a simulated, multiple-choice version of it.

## How the game works

```
WALK THE SHIP → FIND A TERMINAL → READ THE PANEL → RUN THE COMMAND → SOMETHING UNLOCKS
```

Every terminal shows exactly what you need and nothing more:

1. **COMMAND** — the exact command being taught.
2. **WHAT IT DOES** — one short, plain-language explanation.
3. **SYNTAX** — including argument roles like `SOURCE`, `DESTINATION`, `FILE`.
4. **YOUR TASK** — the concrete, observable thing you need to produce.

Type it yourself, or press `Tab` to load the exact command into the prompt
without running it — that's a hint, not autopilot, since you still have to
press `Enter` yourself. After running something, `?` explains the result
once, on request — it never becomes a mandatory screen you have to click
past, and it correctly recognizes a *failed* command as a failure instead of
explaining it as if it had worked.

### The optional recap quiz

When you clear a level (and again when you reach the exit) the game offers a
quick **3-question recap** of what that level taught: press `Y` to take it or
`N` to skip and carry on. Nothing depends on it.

- Each level has its own pool of questions; **3 are drawn at random** every
  time, so a replay isn't the same quiz.
- Most are **"type it" questions**: your answer runs as real bash in a
  throwaway folder and is graded on the *result*, so any command that gets
  there counts (`mkdir -p photos` is as good as `mkdir photos`).
- The rest are **pick a number** questions, with the options shuffled.
- **First wrong answer:** you get a hint (and bash's real error message, if
  there was one) and one more try. **Second wrong answer:** the answer is
  shown and you move on.
- It never costs a life or blocks progress; your running quiz score shows on
  the win screen if you took any.

## What it looks like

These are rendered directly from the game's own drawing code (not mockups)
— what you'd actually see in a terminal.

**The overworld** — walk around, dodge patrols, find the way in:

![Overworld map](screenshots/overworld.png)

**A station** — the command panel is always on screen, never a one-time
card that vanishes after your first visit:

![Terminal panel](screenshots/terminal_panel.png)

**Clearing a level** — a deliberate beat, not a screen you blink and miss:

![Level complete](screenshots/level_complete.png)

**Finishing the game** — a block-letter banner spelled out of the
characters themselves:

![You win](screenshots/you_win.png)

## The map

Eight rooms, one command each, laid out as two rows connected by a shaft —
right along the top, down, then back along the bottom. Every door sits at
its own row — top, middle, or bottom of the room it guards — so the path
actually bends instead of reading as one straight corridor you can walk
without looking:

```
######################################################
#..........####..........####..........####..........#
#.>.!..*.....+...........####..........####...*......#
#......@...####..........####......@...####..........#
#..........####......@...#*##............+.......@...#
#..........####......*.....+....*......####..........#
#..........####..........####..........####..........#
##################################################.###
##################################################+###
##################################################.###
#.#........####..........####..........####..........#
#.#......!.####...........+........*...####..........#
#.#........####......@...####..........####......@...#
#X+....@...#+##..........####......@.................#
#.#......................####..........#+##..........#
#.#........####..........####..........####..........#
######################################################
```

(`@` a terminal, `!` a sign, `*` a coin, `+` a door, `>` the start, `X` the
exit — the actual game draws these in color, with locked/unlocked doors
looking distinct, patrolling enemies, and a facing arrow for the player.)

## Levels, gates, and masking

The eight rooms are split into **three levels**:

- **Level 1** — rooms A, B, C, D (top row)
- **Level 2** — rooms E, F
- **Level 3** — rooms G, H (no coin gate — clear both stations, then collect any coins you skipped and walk out)

A level you haven't reached yet renders as a solid sealed block labeled
**LEVEL n — SEALED**. While neither of the two remaining levels has been
reached, they read as *one* sealed area instead of two separate labels
crammed side by side — that's deliberate, not a rendering bug.

Clearing a level means clearing *every* one of its stations **and**
collecting *every* one of its required coins — not just solving the last
terminal. Do that and a "LEVEL COMPLETE — well played" screen pops up, with
its own highlighted `PRESS SPACE TO CONTINUE` bar. It waits for that
specific key on purpose: an earlier version dismissed on *any* key, which
meant it could vanish inside a single frame if you were still holding an
arrow key from walking into the last coin.

**All eight coins are required to leave.** The two coins that aren't needed
to clear a level (one in room H, one in a hidden closet) are still
mandatory for the win. Walk into `X` with any missing and a **SIGNAL
INCOMPLETE** message tells you how many are left; you stay outside the exit
until you've collected them all.

A level you've already cleared **stays visible for good** — it never goes
back under fog behind you. Finish all three and a big block-letter banner
spells out the win, right on the terminal.

## Install & run

Requires **Python 3.8+**, Bash, and a real terminal window with `curses`
support, at least **112×28** characters (if you see a resize prompt, just
enlarge the window; the game checks its size every frame, so shrinking and
regrowing the window mid-game is safe too). No `pip install` needed to
play — `curses` ships with Python.

```bash
python3 terminalquest.py
```

**Windows:** native Windows terminals don't support `curses`. Run it inside
[WSL](https://learn.microsoft.com/windows/wsl/) with Python 3 installed
instead.

Everything the game creates lives under `~/TerminalQuest_Arcade` — a real
folder on disk, one subfolder per station. Every launch starts a fresh
campaign (progress isn't saved) and rebuilds the training folders; `--reset`
additionally deletes the whole game folder:

```bash
python3 terminalquest.py --reset
```

To put that folder somewhere else entirely:

```bash
TERMINALQUEST_ROOT=/path/to/somewhere python3 terminalquest.py --reset
```

## Controls

| Context | Key | Does |
|---|---|---|
| Overworld | Arrow keys | Move — walk into things to interact with them |
| Overworld | `q` | Quit |
| Overworld | `t` | Toggle high-contrast mode (bold/reverse/underline, no color) |
| At a terminal | *(typing)* | A real bash prompt — nothing is simulated |
| At a terminal | `Tab` | Load the shown command into the prompt, without running it |
| At a terminal | `Enter` | Run the line |
| At a terminal | `?` | Explain the last result (once something's been run) |
| At a terminal | `Esc` | Clear the line, or leave if it's already empty |
| Level-complete screen | `Space` | Continue — shown as its own highlighted bar so it's easy to spot |
| The final "you win" screen | `:wq` then `Enter` | The only way out, once the game's actually over |

| Symbol | Meaning |
|---|---|
| `▶ ▲ ▼ ◀` | You (the arrow points the way you're facing) |
| `x` | A patrol — touching one costs a life |
| `▓` | A locked door |
| `'` | An unlocked door |
| `▣` | An old terminal |
| `*` | Worth grabbing |
| `!` | A sign |
| `X` | The way out |

## How it's built

- **Every room is a real directory.** The overworld map is an in-memory
  grid, but each station's "puzzle" is a real folder under
  `~/TerminalQuest_Arcade/puzzles/`, and every command you type at a
  terminal runs through actual `/bin/bash`.
- **Game logic is UI-free.** The `Game` class in `terminalquest.py` has no
  `curses` in it at all — it's plain Python state plus real subprocess
  calls. The curses front-end (`draw_*` functions and `main()`) is a thin
  layer on top that only renders and reads keys. That split is what makes
  the test suite (and the screenshots above) possible without a real
  terminal.
- **Setup is repair-on-launch, not run-once.** Each station's starting
  files are (re)created from a per-station marker the first time that
  marker is missing — not just "if the folder doesn't exist" — so a folder
  that got deleted, or never finished setting up, gets fixed on the next
  launch instead of staying permanently broken. Real progress (files you've
  already renamed or removed) is never touched once that marker exists.
- **Resize-safe.** Every `draw_*` function tolerates a too-small or
  mid-resize terminal without crashing — `main()` re-checks the window size
  every frame and falls back to a "please resize" screen instead of raising.

## Design philosophy

- **Real outcomes, not answer matching.** Every station checks the actual
  filesystem after your command runs — a real file or folder has to exist,
  get renamed, or disappear. Nothing is pattern-matched against expected
  keystrokes.
- **Guidance without autopilot.** `Tab` inserts the answer; it never
  submits it for you.
- **Explanation at the right moment.** `?` is available *after* you've
  done something, not forced on you before you're allowed to continue.
- **Progress without pressure.** No timers, no speed bonuses, no combo
  penalties, no typing races. Coins and levels exist for pacing, not score
  chasing — see the [design rules](#design-rules-read-this-before-opening-a-pr)
  on why there's no XP or achievement system.
- **Accessible by default.** High-contrast mode (`t`) uses bold, underline,
  and reverse video instead of relying on color alone.

## Safety boundary

TerminalQuest executes commands through your machine's real `/bin/bash`.
Commands run inside a disposable scratch folder — the working directory is
checked after every command and snapped back if anything would escape it —
and a small blocklist rejects a handful of obviously destructive patterns
(`sudo`, `rm -rf /`, fork bombs, `dd`, `mkfs`). **This is not an
operating-system security sandbox.**

Do not:

- run it with administrator or root privileges;
- expose it as a public/shared shell;
- point it at a machine with sensitive data while testing adversarial input; or
- assume the pattern blocklist can catch every possible destructive command.

For adversarial testing, use a disposable VM or container.

## Project layout

```text
terminalquest.py      game logic, the 8-station map, shell runner, and the curses UI
tests/test_game.py    the pytest regression suite (game logic, rendering, stations)
tests/test_quiz.py    the pytest suite for the optional recap quiz (96 tests)
screenshots/          images rendered from the game's own drawing code
README.md             this file
```

The game intentionally keeps logic separate from rendering — movement,
station checks, level gates, and shell execution are all testable without
launching an interactive terminal.

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

210 tests, no real terminal required. What's actually covered:

| Area | What's tested |
|---|---|
| `run_command` | The real-bash execution layer — commands actually shell out to `/bin/bash` from a temp folder, exactly like the live game |
| Map & tiles | Grid layout, wall/door/floor classification, that every room and door lines up |
| Movement | Walking, blocked-by-wall, blocked-by-locked-door, coin pickup, signs |
| Enemies | Patrol movement, collision costing a life, game over at 0 lives |
| Stations | All 8 stations, each solved by exactly one real command; wrong commands never solve anything, and a right command aimed at the wrong target (`mkdir mystuff` instead of `mkdir stash`) gets a nudge instead of a silent fail (including the ones solved by a file's *absence*, which is its own edge case); every launch starts a fresh campaign, so leftover files can't mark a station solved behind the game's back |
| Level gates | Both coin-gated levels require *both* conditions (last station solved **and** every coin collected) in either order; the gate that has no coin requirement; the `":wq"`-quit combo's matching logic |
| The lesson panel | COMMAND/WHAT IT DOES/SYNTAX/YOUR TASK content, `Tab`-to-insert, `?`-to-explain (including that a *failed* command is recognized as an error and never explained as if it had succeeded) |
| Full playthrough | Every station in order, end to end, to the exit |
| Rendering | A `FakeScreen` stand-in renders `draw_base` and friends to plain text at an exact terminal size (including deliberately too-small ones), so a locked tile showing through, or a crash on a shrunk window, gets caught without a real terminal; covers the per-level fog/masking, the congrats screen, and the win banner |
| Recap quiz | Every question's own stated answer passes the real-bash grader, and no do-nothing command (`true`, `pwd`, `echo hi`) passes any typed question; choice questions have exactly one valid answer; 3 distinct questions drawn from the right level's pool, reproducible with a seed; skip / hint-then-retry / reveal-after-two-misses flow; never costs lives or score; key handling and every quiz screen at several window sizes |
| Resize safety | Every `draw_*` function is exercised at several window sizes — comfortably large, far too small, too narrow, too short — and must never raise |

Navigation in the tests uses `terminalquest.walk_to`, a small BFS
pathfinder, instead of hardcoded step counts — so if you rearrange a room or
move a door, the tests should keep passing without being touched.

### Quality approach

How the suite is designed, and what was run before this release:

- **Layered checks.** Unit-level tests for the shell layer, map, movement and
  enemies; state-machine tests for stations and level gates; an end-to-end
  playthrough; and rendering/resize tests. A failure points at one layer.
- **Real behavior, not mocks, where it matters.** Station checks run real
  `/bin/bash` commands from a temp folder, so a test passes only if the actual
  command produces the actual filesystem result.
- **Negative and edge cases.** Wrong commands never solve a station, gates need
  *both* conditions in either order, dangerous commands are blocked, the tracked
  working directory can't leave the game folder, and leftover files from a
  previous session can't solve a station.
- **Resilient to change.** Tests navigate with a BFS pathfinder (`walk_to`)
  rather than hardcoded step counts, so moving a door doesn't break them.
- **Deterministic and fast.** No real terminal, network, or timing
  dependence; the full suite runs in about a second.
- **Release check.** Before the latest release: `pytest` → 210 passed,
  `python3 terminalquest.py --reset --selftest` → `SELFTEST PASSED`, run from a
  clean checkout with no stale `__pycache__`.

- **Measured coverage.** `pytest --cov=terminalquest --cov-branch` reports
  **71%** (1236 statements, 472 branches, 210 tests). The gap is mostly the live
  curses input/main loop and the built-in `--selftest` routine, which pytest
  doesn't execute (the self-test is run separately, above). Game logic, gates,
  station checks and rendering are the well-covered parts.

There's also a lighter, dependency-free smoke test built into the game
itself, useful for a quick sanity check without installing anything:

```bash
python3 terminalquest.py --selftest
```

It drives a full playthrough of all 8 stations, the `Tab`/`?` terminal
affordances, the coin gates, and the win condition, headless.

## Design rules (read this before opening a PR)

This project is deliberately small, and stays that way on purpose:

- **The command set is fixed and tiny**: `ls`, `cd`, `cat`, `mkdir`, `touch`,
  `mv`, `cp`, `rm`. That's it for the base game. No `grep`, `find`, `sort`,
  `wc`, pipes, redirects, environment variables, permissions, or git — those
  are excellent commands and a terrible fit for a five-minute arcade game.
  If your PR teaches a 9th command, it needs a very good reason.
- **One command solves one station.** No multi-step assembly puzzles. The
  player should never sit at a prompt for more than a few seconds figuring
  out what to type.
- **The reference panel stays on screen.** Every terminal always shows
  COMMAND / WHAT IT DOES / SYNTAX / YOUR TASK — not a one-time card that
  disappears after your first visit. `Tab` loads the exact command into the
  prompt (without running it) if you'd rather have it typed for you; `?`
  explains the last result once something's actually run.
- **No dependencies.** Python 3 standard library only (`curses` is built
  in). If your change needs a `pip install` to *play* the game, it's
  probably out of scope (the dev-only test suite is the one exception).
- **Small codebase.** One file, a few thousand lines. Legible in one
  sitting.

If you want to build the bigger version — more commands, XP, achievements,
a full curriculum — fork away, that's a genuinely different (and valid!)
project. This one stays small.

## Contributing

Issues and PRs welcome — this is exactly the kind of project meant to be
poked at, broken, and improved. A few things that'll make a PR easy to
merge:

1. Read the [design rules](#design-rules-read-this-before-opening-a-pr) above
   first — most rejected PRs will be rejected for growing the scope, not
   for code quality.
2. Add or update a test in `tests/test_game.py` (or `tests/test_quiz.py` for the quiz) for any behavior change.
3. Run `pytest` and `python3 terminalquest.py --selftest` before opening
   the PR — both should pass clean.
4. Keep it small. A 20-line diff that fixes one thing beats a 200-line
   diff that also refactors three unrelated things.

Ideas that fit the spirit of this project and would be genuinely welcome:
more rooms/enemies/secrets in the existing map, sound-free visual polish.
Ideas that don't fit: new commands, scoring/achievement systems, external
dependencies, or growing past three small levels.

## Reporting a bug

A useful bug report includes:

- your OS and Python version;
- your terminal application and its window size (`stty size` or just the
  resize prompt the game itself shows you);
- the exact keys or commands you typed, in order;
- what you expected versus what actually happened; and
- the traceback, if one appeared.

A small, reproducible failure makes a great regression test — feel free to
open one alongside a fix.

## License

MIT — see [LICENSE](LICENSE).
