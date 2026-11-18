# Clean-room reverse engineering with agents

The rule for every port: shipped drivers are written from **facts** about the
hardware, never from stock firmware's code (bring-up.md, Ground rules). The
facts are register addresses, bit meanings, init values and tables, timings,
the order in which the hardware needs things done, and protocol constants.
This page is about keeping that rule true when the reverse engineering is
done by agents, and about whether a third agent, a **referee**, should stand
between the two sides.

## What we did, and where it was weak

**YP3: one session did both sides.** The same session read stock in Ghidra
and wrote the drivers. Most of what it took from stock was facts ("12 MHz
tuner reference, as stock", "power off on the 24 MHz clock, as stock"). But
twice code crossed over:

- The mic driver carried stock's generic mic-path pad setup. It muxed four
  pads that are the panel reset and the tuner's I2C on this board, so
  starting the mic held the panel in reset.
- A boot PMU and board setup experiment was transcribed from stock.

Both were caught and removed. Because of them, CLAUDE.md now forbids whole
functions transcribed from stock in the Bluetooth glue.

**TS17: two sessions, files only.**

- A separate Ghidra session works in its own directory and writes
  `findings.md`. The port session writes questions to `questions.md`.
- Messages between the sessions are a bare ping, and the content goes
  through the files.

That fixed the worst of it, but three gaps remained:

1. **Isolation by policy only.** The port session still had the Ghidra MCP
   server configured, and it could read the Ghidra workspace. Nothing
   stopped it from looking except its own discipline.
2. **Findings were narrative, organised by stock's code.** `findings.md`
   grew to about 3,250 lines in 53 sections. It walks routine by routine,
   with pseudocode of stock's control flow, because that is how reverse
   engineering reads. The brief even said "pseudocode describing the
   hardware protocol is fine". Here is a line from it:
   - `while ([0x40050400] & 0x80) [0x40050400] = (low nibble) | 0x80`
   - followed by "stock first runs" this, "returns early unless its own
     software flag ... is 1".

   A driver written from that kind of text follows stock's structure even
   when no line is copied, and structure is what a clean room exists to
   keep out.
3. **The port session writes test tooling from stock as well.** That is
   allowed, because tooling doesn't ship. But it means the session that
   writes the drivers has stock's sequences in its context anyway, and the
   rule again rests on discipline at the moment a driver is written.

**YP3 again, before publishing: the full setup.** An audit of the whole
port found far more than the two cases above. Radio calibration, the
Bluetooth controller's init chain, the power-up and power-off sequences,
the ADC and codec set-up and the SD driver's checks all followed stock
routine by routine, several with comments that said so ("stores are
stock's, in stock's order"). They were cut to stubs and rewritten by three
sessions as described below, with the walls enforced by the machine rather
than by each session's brief.

## Would a referee have helped?

**As a message relay, no.** The pings carried nothing, and the leaks never
happened in the channel. A relay agent would add latency and cost while
guarding the one path that was already safe.

**As a fact gate, yes.** A gate between `findings.md` and the port session
deals with gap 2, which policy can't. Its job is to turn the narrative into a
short fact sheet and to reject what isn't a fact. It is cheap: a small model,
run once per batch of findings, a few minutes each time. The fact sheet it
produces is also a better reference than 3,000 lines of narrative, and its
log is a record of everything that crossed and why.

**Tool isolation matters more than either.** Without it, a referee is
theatre, because the port session could read the source anyway. Isolation
deals with gaps 1 and 3.

So the recommendation is **isolation first, then a light referee as a fact
gate, then a provenance check at commit time**. It is worth it for a port
that will be published or offered upstream. It is overhead for a port with
a public datasheet or an upstream Linux driver, where the facts are already
public.

## The setup

Three roles, each with only the tools it needs:

| Role | Reads | Writes | Must not have |
|---|---|---|---|
| **Analyst** (dirty) | stock images, dumps, Ghidra, the old code; the implementer's code when the drivers disagree | `findings.md`, its own notes | write access to anything the clean side reads or runs |
| **Referee** | `findings.md`, the implementer's questions and reports | `facts.md`, `referee-log.md`, questions back to the analyst | Ghidra, dumps, either tree |
| **Implementer** (clean) | `facts.md`, the hardware, the port's tree, public specs | drivers, `docs/hardware.md`, questions and reports to the referee | Ghidra, dumps, `findings.md`, the old code |

**Mail: one directory per writer.** Each role appends to files in a
directory only it can write: the analyst's findings, the referee's facts
and questions, the implementer's questions and reports (status and
measurements). A directory rather than a single file, because editors
(Claude Code's Edit among them) replace a file by renaming a new one over
it: a write wall around one file breaks its writer, and a read-only mount
of one file shows its reader the old copy.

**Messages:** bare pings only. Content goes through the files. A ping wakes
an idle session; nothing wakes a session that has decided it is finished.

**Who drives:** the analyst sets the pace. The referee and the implementer
work only on what reaches them. Each brief says: after a batch or a work
item, start the next one straight away, and stop only when nothing is left
to act on or a person is needed (a button press, a decision). When the
pipeline stalls, look for mail its reader hasn't answered.

### Walls

Two directions, both enforced by the machine:

- **The clean side must not see the dirty side.** The implementer and the
  referee are denied the Ghidra server, the dumps, the old code, the
  analyst's files, the other sessions' transcripts and the agent's memory.
  Their shells run in the agent's sandbox with everything hidden but their
  inputs and the tools they need. The bench tool, which needs USB, the
  webcam and the device lock, runs outside the sandbox through one fixed
  wrapper.
- **The dirty side must not write anything the clean side reads or runs.**
  That is more than the port's tree: the mail the clean side reads, the
  tools it runs (the `arb` wrapper, the toolchain, host scripts), the
  user-level agent config and skills (loaded into every session), the other
  roles' transcripts (loaded when a session resumes), and the branch the
  clean work lands on.

The analyst usually can't use the agent's sandbox: it drives the bench in
scripts and loops that need USB, the webcam and audio. On Linux a mount
namespace does the job without a second user. Start the analyst's agent
under bubblewrap with the whole system as it is and the protected paths
mounted read-only on top: same user, same devices, the same `/tmp` (device
locks) and `/run` (pings, audio). With `$CR` the clean room and `$PORT` the
port's checkout:

```
bwrap --dev-bind / / \
    --ro-bind "$CR" "$CR" \
    --bind "$CR/analyst" "$CR/analyst" \
    --bind "$CR/mail/from-analyst" "$CR/mail/from-analyst" \
    --ro-bind "$CR/analyst/.claude" "$CR/analyst/.claude" \
    --ro-bind "$PORT/auto-rockbox" "$PORT/auto-rockbox" \
    --ro-bind "$PORT/toolchain" "$PORT/toolchain" \
    --ro-bind "$PORT/rockbox/.git/refs/heads/cleanroom-base" \
              "$PORT/rockbox/.git/refs/heads/cleanroom-base" \
    --ro-bind ~/.claude/settings.json ~/.claude/settings.json \
    --ro-bind ~/.claude/skills ~/.claude/skills \
    -- claude
```

Later mounts win, so the analyst's own directory and outbox come after the
read-only clean room, and its settings after its directory. The read-only
ref file stops commits to the base branch, because git updates a ref by
renaming over it. Then test from inside: every protected path refuses a
write ("Read-only file system"), and the analyst's own work (a worktree, a
build, a device run, a ping) still goes through.

These walls are against mistakes, not an adversary: deliberate side doors
remain (a Ghidra script runs in Ghidra's own process). Separate Unix users
close more of them, at a cost to the bench: USB and audio permissions, the
desktop's audio session, pings between sessions.

### What a fact looks like

One entry per fact, in `facts.md`. For example (the entry's shape is the
point; its order line is made up for the example):

```
F-0112  NOR controller: XIP read setup
  register  0x4002c018
  value     0x80040783 while XIP reads run; field meanings unknown
  order     set before the XIP cache is enabled
  source    stock's init (location in referee-log.md); measured: the live value matches
  trust     value: stock-read + measured; order: GUESS (seen in stock's sequence only)
  answers   Q-031
```

Allowed in a fact:

- addresses and bit fields with their meanings;
- values and tables;
- the order the **hardware** requires, as a list of register writes and
  polls;
- timings, and conditions to poll for;
- protocol constants and frame formats;
- what was measured;
- "GUESS" with what the guess rests on.

Not allowed:

- stock's function names, variable names or call structure ("A calls B,
  then C");
- control flow that belongs to stock's software rather than the hardware
  (its own flags, retries, error paths);
- decompiler output in any form;
- prose that walks through a stock routine line by line.

Gray areas get the "does the hardware need it?" test. If stock waits for bit
7 before each write because the hardware drops writes while busy, that is a
fact ("writes while bit 7 is set are dropped; poll it clear"). If stock
checks its own software flag first, that is stock's design and stays out.

### The referee's pass

1. **Lint, deterministic.** Reject decompiler tokens (`FUN_`, `DAT_`,
   `LAB_`, `uVar`, `iVar`, `param_`, `local_`), C syntax (braces,
   semicolons, `if (`, `while (`), stock symbol names, long hex runs that
   are not labelled tables, and code pointers (source file names, line
   numbers, "instead of", "your helper").
2. **Judgment.** For each finding, ask whether it is a fact about the
   hardware or a description of how stock's code is organised. Rewrite a
   fact into the format above. Send anything else back to the analyst
   through `questions.md`, with the reason, asking for the underlying fact
   ("which register writes, in which order, does the hardware require?").
3. **Log** every decision (accepted, rewritten, rejected) with the finding's
   section, in `referee-log.md`. That log is the record of what crossed.
4. **Questions** from the implementer go through the referee too. A
   question like "how does stock do X?" invites a narrative answer, so the
   referee rephrases it as "what does the hardware need for X?".

### When the drivers disagree

Sometimes the clean driver and the earlier one behave differently on the
same hardware, and the facts don't explain it. Comparing descriptions can
stall there: on the YP3, about 30 facts went into one radio readout that
settled on the earlier driver and stuck on the clean one, while every
check found the two sequences matching as described. Code may then flow
one way, from clean to dirty:

- The analyst clones a fixed implementer commit into its own worktree,
  builds it, and bisects by swapping pieces of the earlier driver in or
  out. Whole files first, to tell whether the cause is in the driver at
  all or elsewhere (board set-up, clocks).
- It names the commit it tested and reports one hardware fact per finding:
  what the hardware needs, and how it was measured ("adding X before the
  first tune to an otherwise unchanged build makes it settle").
- It never points at the implementer's code: no file, function or variable
  names, no line numbers, no "your helper does A, do B instead", no side by
  side of the two codebases. The referee rejects those. Its test: would the
  fact read the same to someone who has seen neither tree?

### Test tooling

Payloads and gadgets that follow stock's or the ROM's sequences are allowed,
because they don't ship. Keep them on the tooling side:

- write them from facts marked "tooling", or have the analyst write them;
- keep them in the host tool's tree, never under `firmware/target/`;
- say in a comment which sequence they follow.

### At commit time and before release

- A lint on driver diffs: decompiler tokens, stock symbol names, comments
  that cite a stock function as the source of logic rather than of a fact,
  and large unlabelled tables.
- The release's code quality audit (release.md) checks provenance: every
  register header says where its facts came from (bring-up.md, "One home
  for every constant"). Every driver can be traced to `facts.md` entries or
  measurements.

## Several players on one SoC

Players that share a SoC share one analyst, one referee and one mailbox
(the TS17 and the TS28 on the Actions Lark), with a port session each:

- Every fact names its scope: `chip` (any board) or `board: NAME`. A
  board fact of one player is never used for another; the port asks
  for its own board's.
- The port sessions append to one question file with ids in one
  sequence: re-read the file just before appending, and the later of two
  writers renumbers.
- A new port's first entry is a status entry naming its session, so the
  referee pings the right port for its facts; the two port sessions may
  talk to each other directly (both are clean).
- The new player's stock dump goes where the analyst imports from, as a
  backup made by the port's tools and never opened on the clean side.

## Bringing an existing port in line

For a port already under way, as the TS17 and the YP3 were:

1. **Wall the sessions** as above.
2. **Audit every function** in the target code, one read-only pass per
   area, and class each as fact (addresses, values and the order the
   hardware needs), port (follows a stock routine's structure), ours, or
   unclear. The "does the hardware need it?" test settles the unclear ones;
   when in doubt, cut. Comments are the quickest pointers: "as stock", "in
   stock's order", a stock routine's address next to a block of code.
3. **Cut on a base branch.** Delete the ports and the unclear code that
   leans port. Leave one stub per work item,
   `TODO(clean-room Wn): <the goal in plain words>`, that compiles and
   returns a permissive value. Scrub comments of stock routine names,
   addresses and narration; keep register, ROM entry, data and hook
   addresses, which are facts. The branch must build.
4. **Give the implementer that branch with fresh history** (an archive in a
   new repository), so the cut code isn't in its log.
5. **Write the work list in functional terms** ("the card reads and writes
   with DMA", "power-off leaves the player off until the key"), one item per
   stub, and have the referee vet it once: its author has seen stock.
6. **Run one referee pass over any existing findings** to start `facts.md`.
   From then on the implementer reads only that.
7. **Bring the work back** with `git format-patch` onto the base branch.
   Publish from it once no stub is left and the release checks pass again.

## With Claude Code (2.1.x, November 2026)

The walls above in Claude Code's terms, and the traps that cost time:

- Each role's `.claude/settings.json`: `permissions.deny` with
  `Read(//abs/path/**)` and `Edit(...)` rules and the MCP servers by name
  (`mcp__ghidra`; servers configured for the user must be denied per
  project); `"autoMemoryEnabled": false`; `sandbox.enabled` with
  `filesystem.denyRead` (home), `allowRead` (the role's inputs),
  `allowWrite` (its outputs), `allowUnsandboxedCommands: false`, and
  `excludedCommands` for the bench wrapper.
- A Read deny rule with a wildcard in the middle of a path
  (`//tmp/x/-home-a*/**`) makes the sandbox refuse to run programs
  (`python3` and `sh` fail with "permission denied"). Name the directories.
- An excluded command runs outside the sandbox only when the whole call is
  that command: a pipe, a `>` redirect or a `cd` to another directory in
  the same call puts it back inside. Make the wrapper say so when it finds
  itself sandboxed (no `/dev/bus/usb`).
- Allow rules are ignored until the trust dialog has been accepted in that
  directory.
- Edit replaces files by rename, and refuses to write through a symlink (it
  names the target instead).
