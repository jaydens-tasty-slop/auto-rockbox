"""SBC as an A2DP sink sees it: frame headers and their CRC, and a stream
configuration (the codec information element, one value per field) in plain
numbers, with the checks a strict sink applies to it."""

from __future__ import annotations

from dataclasses import dataclass

RATES = (16000, 32000, 44100, 48000)
MODES = ("MONO", "DUAL_CHANNEL", "STEREO", "JOINT_STEREO")
ALLOCATIONS = ("LOUDNESS", "SNR")       # the frame header's bit: 0 loudness, 1 SNR
SYNC = 0x9C
MAX_BITPOOL = 53                        # the virtual sink's limit (a typical headset's)

# The codec information element's flags (one bit per choice) -> header values
_IE_RATE = {8: 16000, 4: 32000, 2: 44100, 1: 48000}
_IE_MODE = {8: 0, 4: 1, 2: 2, 1: 3}
_IE_BLOCKS = {8: 4, 4: 8, 2: 12, 1: 16}
_IE_SUBBANDS = {2: 4, 1: 8}
_IE_ALLOC = {1: 0, 2: 1}


def _crc8_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ 0x1D) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        table.append(c)
    return table


_CRC8 = _crc8_table()


@dataclass(frozen=True)
class Frame:
    """An SBC frame header"""

    rate: int
    blocks: int
    mode: int                   # index into MODES
    alloc: int                  # index into ALLOCATIONS
    subbands: int
    bitpool: int

    @property
    def channels(self) -> int:
        return 1 if self.mode == 0 else 2

    @property
    def samples(self) -> int:
        return self.blocks * self.subbands

    @property
    def length(self) -> int:
        """The whole frame's bytes"""
        if self.mode < 2:
            bits = self.blocks * self.channels * self.bitpool
        elif self.mode == 2:
            bits = self.blocks * self.bitpool
        else:
            bits = self.subbands + self.blocks * self.bitpool
        return 4 + (4 * self.subbands * self.channels) // 8 + (bits + 7) // 8


def frame_header(data: bytes, off: int = 0) -> Frame | str:
    """The header of the frame at OFF, or what is wrong with it"""
    if len(data) - off < 4:
        return f"short SBC frame ({len(data) - off} bytes)"
    if data[off] != SYNC:
        return f"bad SBC sync byte 0x{data[off]:02x}"
    b = data[off + 1]
    return Frame(rate=RATES[b >> 6], blocks=4 * (1 + ((b >> 4) & 3)), mode=(b >> 2) & 3,
                 alloc=(b >> 1) & 1, subbands=8 if b & 1 else 4, bitpool=data[off + 2])


def crc_ok(frame: bytes, f: Frame) -> bool:
    """The header CRC-8 (polynomial 0x1D, initial 0x0F) over header bytes 1-2,
    the join bits and the scale factors"""
    nbits = 16 + (f.subbands if f.mode == 3 else 0) + 4 * f.subbands * f.channels
    data = bytes(frame[1:3]) + bytes(frame[4:4 + (nbits - 16 + 7) // 8])
    crc = 0x0F
    for i in range(nbits // 8):
        crc = _CRC8[crc ^ data[i]]
    if nbits % 8:
        octet = data[nbits // 8]
        for _ in range(nbits % 8):
            bit = ((octet ^ crc) & 0x80) >> 7
            crc = ((crc & 0x7F) << 1) ^ (0x1D if bit else 0)
            octet = (octet << 1) & 0xFF
    return crc == frame[3]


@dataclass(frozen=True)
class Config:
    """A configured stream: one value per field, None where the element did
    not name exactly one"""

    rate: int | None
    mode: int | None
    blocks: int | None
    subbands: int | None
    alloc: int | None
    min_bitpool: int
    max_bitpool: int

    def frame(self, bitpool: int) -> Frame | None:
        """The header a frame of this configuration has at BITPOOL"""
        if (self.rate is None or self.mode is None or self.blocks is None
                or self.subbands is None or self.alloc is None):
            return None
        return Frame(self.rate, self.blocks, self.mode, self.alloc, self.subbands, bitpool)

    def mismatches(self, f: Frame) -> list[str]:
        """How frame header F departs from this configuration"""
        bad = [k for k in ("rate", "mode", "blocks", "subbands", "alloc")
               if getattr(f, k) != getattr(self, k)]
        if not self.min_bitpool <= f.bitpool <= self.max_bitpool:
            bad.append(f"bitpool {f.bitpool} outside {self.min_bitpool}..{self.max_bitpool}")
        return bad

    def __str__(self) -> str:
        f = self.frame(self.max_bitpool)
        if f is None:
            return f"invalid SBC configuration {self!r}"
        kbps = f.length * 8 * f.rate / f.samples / 1000
        return (f"SBC {f.rate} Hz {MODES[f.mode]} ({f.channels} ch), {f.blocks} blocks, "
                f"{f.subbands} subbands, {ALLOCATIONS[f.alloc]}, bitpool "
                f"{self.min_bitpool}..{self.max_bitpool} ({f.length}-byte frames, "
                f"{kbps:.0f} kbit/s at max bitpool)")


def _one(v: int) -> bool:
    return v != 0 and v & (v - 1) == 0


@dataclass(frozen=True)
class Element:
    """An SBC codec information element's fields as numbers (the flag
    fields have one bit per choice)"""

    sampling_frequency: int
    channel_mode: int
    block_length: int
    subbands: int
    allocation_method: int
    min_bitpool: int
    max_bitpool: int

    def config(self) -> Config:
        return Config(_IE_RATE.get(self.sampling_frequency), _IE_MODE.get(self.channel_mode),
                      _IE_BLOCKS.get(self.block_length), _IE_SUBBANDS.get(self.subbands),
                      _IE_ALLOC.get(self.allocation_method), self.min_bitpool,
                      self.max_bitpool)

    def problem(self) -> tuple[int, str] | None:
        """What a strict sink rejects in it as a SET_CONFIGURATION: (A2DP
        error code, why), or None if it is acceptable"""
        checks = (
            (_one(self.sampling_frequency), 0xC3, "sampling frequency must be one value"),
            (self.sampling_frequency & 0x3, 0xC4, "sampling frequency not 44.1/48 kHz"),
            (_one(self.channel_mode), 0xC5, "channel mode must be one value"),
            (_one(self.block_length), 0xDD, "block length must be one value"),
            (_one(self.subbands), 0xC7, "subbands must be one value"),
            (_one(self.allocation_method), 0xC9, "allocation method must be one value"),
            (2 <= self.min_bitpool <= self.max_bitpool, 0xCB,
             "minimum bitpool < 2 or > maximum"),
            (self.max_bitpool <= 250, 0xCD, "maximum bitpool > 250"),
            (self.max_bitpool <= MAX_BITPOOL, 0xCE,
             f"maximum bitpool above the sink limit {MAX_BITPOOL}"))
        return next(((code, why) for ok, code, why in checks if not ok), None)
