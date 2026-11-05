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
deals with gaps 1 and 3 and costs one settings file.

So the recommendation is **isolation first, then a light referee as a fact
gate, then a provenance check at commit time**. It is worth it for a port
that will be published or offered upstream. It is overhead for a port with
a public datasheet or an upstream Linux driver, where the facts are already
public.

## The setup

Three roles, each with only the tools it needs:

| Role | Reads | Writes | Must not have |
|---|---|---|---|
| **Analyst** (dirty) | stock images, dumps, Ghidra | `findings.md` (free form), answers in `questions.md` | write access to the port's tree |
| **Referee** | `findings.md`, `questions.md` | `facts.md`, `referee-log.md`, rejections back to the analyst | Ghidra, dumps, the port's tree |
| **Implementer** (clean) | `facts.md`, the hardware, the port's tree | drivers, `docs/hardware.md`, questions | Ghidra, dumps, `findings.md` |

**Enforce it in each session's settings.** In the implementer's
`.claude/settings.json`, deny rules cover the Ghidra MCP server (for
example `mcp__ghidra`) and reads of the analyst's directory and of the
stock dumps. The referee is denied everything but its two inputs and two
outputs. A separate Unix user or container is stronger still, if the
bench allows one.

**Messages:** bare pings only, as now. Content goes through the files.

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
   semicolons, `if (`, `while (`), stock symbol names, and long hex runs that
   are not labelled tables.
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

## Bringing an existing port in line

For a port already under way, as the TS17 was:

1. Deny the implementer session the Ghidra MCP server and the analyst's
   files.
2. Run one referee pass over the existing `findings.md` to produce
   `facts.md`. From then on the implementer reads only that.
3. Audit the drivers already written for structure that mirrors stock
   routines. Rewrite them from `facts.md` where it does.
