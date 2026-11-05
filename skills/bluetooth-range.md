# Bluetooth range

How far the player streams before it stutters, how it degrades, and whether
it comes back by itself after the link drops. Range depends on the radio
path (TX power, RX sensitivity, RF set-up), the link layer (packet types,
EDR, AFH, power control, retransmission), the codec's adaptation (bitpool)
and the board's own noise. You can only tell them apart with numbers, and
only against stock: a house is not a lab. So **always walk stock and Rockbox
on the same route, on the same day, with the same peer**.

There are two walks. They test different directions of the link.

## 1. Headset walk: the player stays, the listener walks

The player sits on the desk, on USB, running a dev build with the serial
shell (`usb=1`), and streams to real headphones. A person wears them and
walks a fixed route (desk, stairs, kitchen, garage, back), saying or noting
where the sound first stutters, where it cuts out, and where it comes back.
The PC logs the player's side live:

```
arb yp3 bt-walk build/bt/walk-0429 --minutes 15
```

It holds the dev shell for the whole walk. It writes every new dev-log line
and the BT and audio state changes, and polls the link stats: role, RSSI, TX
power, packet types, and the player's stats lines with bitpool, ACL buffer
waits and clock restarts. Every line is stamped with the wall clock. It ends
when the headset is back after a drop and playback has resumed (or stayed
paused for 20 s), or at the time limit.

What it shows:

- where the bitpool starts falling and the buffer waits climb: the margin
  ran out;
- whether the link drops (`0x08` connection timeout, `0x22` LMP response
  timeout) or just starves;
- after a drop, how long until the player re-pages, the link is back, and
  audio resumes, untouched. Many headsets do not page again after a loss,
  so the player must.

Unplugging USB at the desk partway through shows whether the cable matters
(it did not on the YP3).

## 2. Player walk: the listener stays, the player walks

The PC is the A2DP sink (BlueZ and PipeWire). Route the stream into a null
sink so nobody hears it. A person carries the player in a pocket along the
route and back. The PC records everything:

```
sudo arb bt capture build/bt/pocket-1028          # HCI trace + RSSI every 2 s (root)
arb bt gaplog build/bt/pocket-1028/gaps.txt         # stream gaps, live
arb bt watch build/bt/pocket-1028/conn.txt          # link state; pages after a drop
```

With USB unplugged the player cannot log live, so set `sdlog=1` in its dev
config. It appends its log to the card, which you read back afterwards
(`arb yp3 card-run 'cp "$MP/yp3dev/devlog.txt" build/bt/pocket-1028/'`).
Stock players often cannot connect to a PC themselves, and a PC refuses
their role switch: `arb bt page` finds, trusts and connects a discoverable
player from the PC side.

Afterwards:

```
arb bt flow build/bt/pocket-1028/pc.btsnoop         # KB/s, frames per packet, stalls
arb bt report build/bt/pocket-1028                  # one timeline + summary
```

The report merges the gap log, RSSI, media flow, connection log and player
log. It gives the seconds silent, the gaps by size, the drops, the time from
drop to link and to audio, and the RSSI range.

## Reading the numbers

- **RSSI is relative** (BlueZ reports it against its golden range) and
  depends on where the player sits in a pocket. Compare stock and ours at
  the same spot, minutes apart. If both read the same RSSI and ours loses
  more, the problem is not TX power: look at our receiver or link layer.
- **Bitpool and throughput tell link margin.** An A2DP source that can't
  send lowers its bitpool. The YP3 fell from 53 to 24 where stock held 35
  with five frames per packet.
- **The packet shape matters.** Stock sent one 2-DH5 baseband packet per
  media packet (EDR, five SBC frames). Ours sent three BR DH5s until EDR
  worked, and that cost range.
- **A/B at one spot beats a walk** for a single variable: switch the
  variable every minute (a dev config toggle), stand still, and interleave
  the phases. Conditions drift (doors, Wi-Fi, people), so look for a
  difference larger than the drift between repeats of the same phase.
- **Board noise counts.** Clock harmonics in 2402-2480 MHz (bus, NOR, audio
  PLL, backlight PWM) cost margin. Compare which clocks stock leaves on
  while streaming.
- Write each walk's route, peer, builds and results into the port's
  Bluetooth doc. One line per walk is enough, with the run directory.

## What good looks like

For a release ([release.md](release.md)):

- Headset walk: the first stutter no nearer than stock's on the same route,
  and after a drop the player re-pages and resumes within 30 s with nobody
  touching it.
- Player walk: seconds silent on the route no more than 1.5 times stock's,
  and a reconnect after the return without a key press.
- No crash, hang or stuck state across five drop-and-return cycles in a row.
