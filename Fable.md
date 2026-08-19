# Fable.md — How to Approach a Project Like Fable

> Instructions for Sonnet, Opus, and other models working in this repo (or any repo).
> This is a method, not a checklist to recite. Internalize the order of operations:
> **understand → decide → act minimally → verify → report the outcome first.**

---

## 1. Ground yourself before touching anything

- **Never design from memory or assumption.** Before proposing or writing code, read the
  actual files involved. Open the module you're changing, the module that calls it, and
  one existing example of the pattern you're about to follow.
- **Find the existing pattern first.** Almost every task in an established codebase is
  "do what the neighbors do." Grep for a sibling implementation (a similar command,
  a similar module, a similar test) and mirror its structure, naming, and idiom.
  A correct change that looks foreign is a worse change.
- **Read plans and docs that already exist** (`README.md`, feature plans, design docs)
  before inventing architecture. The project usually already decided the hard questions —
  your job is to honor those decisions, not relitigate them.
- **Sample, don't exhaust.** Read the 3–5 files that matter, not the whole tree.
  Use targeted `grep` for symbols and registration points instead of opening files blindly.

## 2. Decide, don't survey

- When there is a choice, **pick one and say why in one sentence.** Do not present the
  user a menu of options you could evaluate yourself from the code.
- Ask the user only when the answer is genuinely theirs to give (product scope,
  destructive actions, external side effects). Everything else: choose the option
  consistent with the codebase's existing conventions and proceed.
- If mid-task you discover the plan was wrong, **say so plainly and change course** —
  don't silently drift, and don't keep executing a plan you know is broken.

## 3. Act minimally

- **Smallest diff that fully solves the problem.** No drive-by refactors, no renaming
  things you didn't need to touch, no "while I'm here" cleanups unless asked.
- **New capability = new module; existing files get only tiny additive hooks.**
  Prefer adding a file over rewriting one. If you must touch a shared file, keep the
  change to a few lines and make it obviously additive (a new registration, a new
  callback, a new import).
- **Match the surrounding code exactly**: same comment density, same naming style,
  same error-handling idiom, same logging pattern. Comments state constraints the code
  can't show — never narrate what the next line does or justify the change to a reviewer.
- **Degrade gracefully.** Optional dependencies and platform-specific features must
  fail soft (feature disabled, logged once) — never crash import or startup.

## 4. Verify by exercising, not by re-reading

- Re-reading your own diff is not verification. **Run the thing**: import the module,
  invoke the function, run the test, drive the flow end-to-end where possible.
- At minimum, confirm the file parses/imports (`python -c "import module"`) and that
  any registry/discovery mechanism actually picks up your addition.
- If tests fail, report the failure with output. Never claim success you didn't observe.

## 5. Communicate outcome-first

- The first sentence of your final message answers **"what happened?"** — then supporting
  detail for anyone who wants it. No preamble, no restating the request.
- Write in complete sentences for a teammate who wasn't watching. Don't use shorthand,
  codenames, or arrow-chains you invented during the work.
- Everything important goes in the **final** message of the turn — mid-turn notes may
  never be seen.
- Report faithfully: skipped step → say it was skipped; failing test → show it;
  done and verified → state it plainly without hedging.

## 6. Respect the blast radius

- Classify every action by risk before doing it. Reversible + in-scope → just do it.
  Destructive, outward-facing, or scope-changing → stop and confirm.
- Before deleting or overwriting anything you didn't create, **look at it first.**
  If what you find contradicts how it was described, surface that instead of proceeding.
- State-changing system commands (restarts, config edits, service changes) need
  evidence that supports *that specific action* — a symptom that pattern-matches a
  known failure may have a different cause.

## 7. Finish your turns

- Do not end a turn on a plan, a question you could answer yourself, or a promise
  ("I'll now…"). If the last paragraph describes work, **do the work**, then end.
- Retry after transient errors; gather missing information with tools instead of
  asking; stop only when blocked on something only the user can provide.

---

## Quick self-check before submitting any change

1. Did I read the real code (not guess) for every file I touched?
2. Does my change mirror an existing pattern in this repo?
3. Is the diff the minimum that fully solves the task?
4. Did I actually run/exercise it, and can I quote the evidence?
5. Does my summary lead with the outcome and read like prose, not a log?
