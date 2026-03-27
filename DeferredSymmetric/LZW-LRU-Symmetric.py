#!/usr/bin/env python3
"""
LZW Compression with Symmetric LRU (Deferred Addition)

Implements LZW compression with LRU (Least Recently Used) eviction policy where
the decoder maintains its own LRU tracker that perfectly mirrors the encoder's.
NO eviction signals are sent in the bitstream — the bitstream contains only
LZW codewords and an EOF marker.

============================================================================
THE PROBLEM WITH STANDARD LZW + LRU
============================================================================

In standard LZW, the encoder adds entry (current_match + next_char) immediately
at step j, but the decoder can only compute this entry at step j+1 as
(prev_decoded + current_decoded[0]). This one-step offset means:

  1. When the dictionary is full and the encoder evicts an LRU entry at position P,
     the decoder hasn't evicted yet (it's one entry behind).
  2. If the encoder immediately reuses code P (for the new entry), the decoder
     still sees the OLD entry at P → corruption.
  3. The original code solved this with EVICT_SIGNAL: a special codeword that
     tells the decoder "I evicted code P, here's the new value."

============================================================================
THE SOLUTION: DEFERRED ADDITION
============================================================================

Both sides compute new entries using the DECODER's formula:

    new_entry = prev_output + current_output[0]

and add them at the same logical point (after output/decode). The encoder
"defers" its addition by one step to match the decoder's natural timing.

Why this works:
  - In standard LZW, the encoder adds (s_j + next_char) at step j.
  - The decoder computes (prev + s_j[0]) at step j, which equals (s_{j-1} + s_j[0]).
  - But s_j[0] = next_char from step j-1 (since s_j starts with the leftover char).
  - So prev + s_j[0] = s_{j-1} + next_char_{j-1} = the entry from step j-1.
  - If we make the ENCODER also add the entry from step j-1 at step j, both sides
    add the same entry at the same time.

Key property: this doesn't change WHICH entries are added — only WHEN. Entry c_{j-1}
becomes available at step j instead of step j-1. For large files, this has negligible
impact on compression ratio.

============================================================================
PROOF OF SYNCHRONIZATION
============================================================================

Both sides perform identical LRU operations in identical order:

  Encoder step j: output code(s_j), LRU.use(s_j), add entry, LRU.use(entry)
  Decoder step j: read code → s_j,  LRU.use(s_j), add entry, LRU.use(entry)

  Entry at step j = prev_output + s_j[0] (same for both sides)

Since both start from the same initial state and perform identical operations,
their LRU states are always identical. When the dictionary is full, both evict
the same entry. Both add the same new entry at the evicted position.

BONUS: The codeword==next_code special case from standard LZW is eliminated!
Since both sides have identical dictionaries at all times, the decoder always
has every entry the encoder references.

============================================================================
BITSTREAM FORMAT
============================================================================

  Header:
    [8 bits: min_bits] [8 bits: max_bits] [16 bits: alphabet_size]
    [8 bits × alphabet_size: alphabet characters]

  Body:
    Variable-width codewords (min_bits to max_bits wide)

  Footer:
    [EOF_CODE at current bit width]

  No EVICT_SIGNAL. No eviction metadata. Just codewords.

============================================================================
USAGE
============================================================================

    Compress:   python3 LZW-LRU-Symmetric.py compress input.txt output.lzw --alphabet ascii
    Decompress: python3 LZW-LRU-Symmetric.py decompress input.lzw output.txt

    Options:
      --alphabet    ascii | extendedascii | ab
      --min-bits    Starting bit width (default 9)
      --max-bits    Maximum bit width (default 16)
"""

import sys
import argparse
from typing import TypeVar, Generic, Optional, Dict

# Predefined alphabets
ALPHABETS = {
    'ascii': [chr(i) for i in range(128)],          # Standard ASCII (0-127)
    'extendedascii': [chr(i) for i in range(256)],  # Extended ASCII (0-255)
    'ab': ['a', 'b']                                 # Binary alphabet for testing
}


# ============================================================================
# BIT-LEVEL I/O CLASSES
# ============================================================================
# LZW uses variable-width codes (9 bits, 10 bits, etc.) but files are stored
# as bytes (8 bits). These classes handle the bit-to-byte conversion.

class BitWriter:
    """
    Writes variable-width integers as a stream of bits to a binary file.

    Accumulates bits in an integer buffer. When the buffer has ≥8 bits,
    extracts and writes one byte at a time.
    """

    def __init__(self, filename):
        self.file = open(filename, 'wb')
        self.buffer = 0   # Integer accumulating bits
        self.n_bits = 0   # Count of bits in buffer not yet written

    def write(self, value, num_bits):
        """Write 'num_bits' bits from 'value' to output."""
        self.buffer = (self.buffer << num_bits) | value
        self.n_bits += num_bits
        while self.n_bits >= 8:
            self.n_bits -= 8
            byte = self.buffer >> self.n_bits
            self.file.write(bytes([byte]))
            self.buffer &= (1 << self.n_bits) - 1

    def close(self):
        """Flush remaining bits (zero-padded) and close file."""
        if self.n_bits > 0:
            byte = self.buffer << (8 - self.n_bits)
            self.file.write(bytes([byte]))
        self.file.close()


class BitReader:
    """
    Reads variable-width integers from a stream of bits in a binary file.

    Mirrors BitWriter — accumulates bytes into buffer, extracts requested bits.
    """

    def __init__(self, filename):
        self.file = open(filename, 'rb')
        self.buffer = 0
        self.n_bits = 0

    def read(self, num_bits):
        """Read 'num_bits' bits from input. Returns None at EOF."""
        while self.n_bits < num_bits:
            byte_data = self.file.read(1)
            if not byte_data:
                return None
            self.buffer = (self.buffer << 8) | byte_data[0]
            self.n_bits += 8
        self.n_bits -= num_bits
        value = self.buffer >> self.n_bits
        self.buffer &= (1 << self.n_bits) - 1
        return value

    def close(self):
        self.file.close()


# ============================================================================
# LRU TRACKER DATA STRUCTURE
# ============================================================================

K = TypeVar('K')

class LRUTracker(Generic[K]):
    """
    O(1) LRU tracker using doubly-linked list + HashMap.

    - use(key):      Mark key as most recently used (add if new)   — O(1)
    - find_lru():    Return least recently used key                 — O(1)
    - remove(key):   Remove key from tracking                      — O(1)
    - contains(key): Check if key is tracked                       — O(1)

    Uses sentinel head/tail nodes to eliminate edge cases in list operations.
    Only tracks non-alphabet dictionary entries (single-char entries are permanent).
    """
    __slots__ = ('map', 'head', 'tail')

    class Node:
        __slots__ = ('key', 'prev', 'next')
        def __init__(self, key: Optional[K]) -> None:
            self.key = key
            self.prev = None
            self.next = None

    def __init__(self) -> None:
        self.map: Dict[K, 'LRUTracker.Node'] = {}
        self.head = self.Node(None)  # Sentinel: most recently used end
        self.tail = self.Node(None)  # Sentinel: least recently used end
        self.head.next = self.tail
        self.tail.prev = self.head

    def use(self, key: K) -> None:
        """Mark key as recently used. Adds key if not present."""
        node = self.map.get(key)
        if node is not None:
            self._remove_node(node)
            self._add_to_front(node)
        else:
            node = self.Node(key)
            self.map[key] = node
            self._add_to_front(node)

    def find_lru(self) -> Optional[K]:
        """Return least recently used key, or None if empty."""
        if self.tail.prev == self.head:
            return None
        return self.tail.prev.key

    def remove(self, key: K) -> None:
        """Remove key from tracking."""
        node = self.map.pop(key, None)
        if node is not None:
            self._remove_node(node)

    def contains(self, key: K) -> bool:
        """Check if key is being tracked."""
        return key in self.map

    def _add_to_front(self, node) -> None:
        node.next = self.head.next
        node.prev = self.head
        self.head.next.prev = node
        self.head.next = node

    def _remove_node(self, node) -> None:
        node.prev.next = node.next
        node.next.prev = node.prev


# ============================================================================
# SHARED DICTIONARY MANAGEMENT
# ============================================================================
# Both encoder and decoder call this IDENTICAL function to add entries.
# This is the heart of the symmetric approach — by running the same code,
# both sides maintain identical dictionaries and LRU states.

def dict_add_entry(fwd, rev, entry, next_code, max_code, lru, code_bits, max_bits, threshold):
    """
    Add a new entry to the bidirectional dictionary.
    If full, evict LRU entry and reuse its code.

    CRITICAL: Both encoder and decoder must call this with the same arguments
    at the same logical step to maintain synchronization.

    Args:
        fwd:       Forward dictionary (string → code)
        rev:       Reverse dictionary (code → string)
        entry:     New string to add
        next_code: Next available code for non-full dictionary
        max_code:  Maximum valid code + 1 (capacity)
        lru:       LRU tracker instance
        code_bits: Current bit width
        max_bits:  Maximum bit width
        threshold: Threshold for bit width increment (2^code_bits)

    Returns:
        (next_code, code_bits, threshold) — updated values
    """
    # Skip if entry already exists — avoids wasting dictionary slots (O(1) check)
    # Inherent issue with deferred addition: same entry could be added multiple times if it appears in multiple prev+current combinations.
    if entry in fwd:
        return next_code, code_bits, threshold
    
    if next_code < max_code:
        # ── Dictionary NOT full: add at next available code ──
        fwd[entry] = next_code
        rev[next_code] = entry
        lru.use(entry)
        next_code += 1
        # Check if bit width needs to increase AFTER incrementing
        # so the returned code_bits is immediately correct for the next read/write.
        # (Checking before incrementing leaves a gap where next_code has crossed
        # the threshold but code_bits hasn't updated yet — causing EOF desync.)
        if next_code >= threshold and code_bits < max_bits:
            code_bits += 1
            threshold <<= 1
    else:
        # ── Dictionary FULL: evict LRU entry, reuse its code ──
        lru_entry = lru.find_lru()
        if lru_entry is not None:
            lru_code = fwd[lru_entry]
            del fwd[lru_entry]
            del rev[lru_code]
            lru.remove(lru_entry)
            fwd[entry] = lru_code
            rev[lru_code] = entry
            lru.use(entry)
            # next_code unchanged — we reused the evicted slot

    return next_code, code_bits, threshold


# ============================================================================
# COMPRESSION (Encoder with Deferred Addition)
# ============================================================================

def compress(input_file, output_file, alphabet_name, min_bits=9, max_bits=16):
    """
    Compress a file using LZW with symmetric LRU and deferred addition.

    Deferred addition means new dictionary entries are computed as:
        entry = prev_output + current_output[0]
    instead of the standard:
        entry = current_match + next_char

    These are the same value deferred by one step, which synchronizes
    the encoder's and decoder's dictionary additions.

    Algorithm per output step j (j ≥ 2):
      1. Find longest match in dictionary → s_j
      2. Write code(s_j) at current bit width
      3. LRU.use(s_j) if it's a tracked (non-alphabet) entry
      4. Compute entry = prev_output + s_j[0]
      5. Add entry to dictionary (evicting LRU if full)
      6. LRU.use(entry)
      7. Update prev_output = s_j

    Args:
        input_file:    File to compress
        output_file:   Compressed output file
        alphabet_name: Which alphabet to use
        min_bits:      Starting bit width for codes (default 9)
        max_bits:      Maximum bit width (default 16)
    """
    alphabet = ALPHABETS[alphabet_name]
    valid_chars = set(alphabet)

    # ── Write header ──
    writer = BitWriter(output_file)
    writer.write(min_bits, 8)
    writer.write(max_bits, 8)
    writer.write(len(alphabet), 16)
    for char in alphabet:
        writer.write(ord(char), 8)

    # ── Initialize dictionary ──
    # Bidirectional: fwd (string→code) for encoder lookups, rev (code→string) for consistency
    fwd = {char: i for i, char in enumerate(alphabet)}
    rev = {i: char for i, char in enumerate(alphabet)}

    # Code allocation (no EVICT_SIGNAL — all codes available for dictionary):
    #   0 to alphabet_size-1: alphabet characters
    #   alphabet_size: EOF marker
    #   alphabet_size+1 to max_size-1: dictionary entries
    EOF_CODE = len(alphabet)
    max_size = 1 << max_bits
    max_code = max_size       # Upper bound (exclusive) for dictionary codes
    next_code = len(alphabet) + 1

    # Variable-width encoding
    code_bits = min_bits
    threshold = 1 << code_bits  # Increment code_bits when next_code reaches this

    # LRU tracker for non-alphabet entries
    lru = LRUTracker()

    # ── Read and compress ──
    with open(input_file, 'rb') as f:
        first_byte = f.read(1)
        if not first_byte:
            # Empty file: just write EOF
            writer.write(EOF_CODE, min_bits)
            writer.close()
            return

        first_char = chr(first_byte[0])
        if first_char not in valid_chars:
            raise ValueError(f"Byte {first_byte[0]} at position 0 not in alphabet")

        current = first_char     # Current match being extended
        prev_output = None       # Previous step's output (for deferred entry)
        pos = 1

        # ── Main compression loop ──
        while True:
            byte_data = f.read(1)
            if not byte_data:
                break

            char = chr(byte_data[0])
            if char not in valid_chars:
                raise ValueError(f"Byte {byte_data[0]} at position {pos} not in alphabet")
            pos += 1

            combined = current + char

            if combined in fwd:
                # Phrase exists — keep extending
                current = combined
            else:
                # ── Output code for current match ──
                writer.write(fwd[current], code_bits)

                # Mark as recently used (only tracked multi-char entries)
                if lru.contains(current):
                    lru.use(current)

                # ── Deferred addition ──
                # Add entry = prev_output + current[0]
                # This is the entry from the PREVIOUS step, matching decoder timing.
                # current[0] is the first char of this step's match, which equals
                # the leftover char from the previous step's match failure.
                if prev_output is not None:
                    entry = prev_output + current[0]
                    next_code, code_bits, threshold = dict_add_entry(
                        fwd, rev, entry, next_code, max_code,
                        lru, code_bits, max_bits, threshold
                    )

                prev_output = current
                current = char

    # ── Output final match ──
    writer.write(fwd[current], code_bits)

    if lru.contains(current):
        lru.use(current)

    # Add the deferred entry from previous step
    if prev_output is not None:
        entry = prev_output + current[0]
        next_code, code_bits, threshold = dict_add_entry(
            fwd, rev, entry, next_code, max_code,
            lru, code_bits, max_bits, threshold
        )

    # ── Write EOF ──
    writer.write(EOF_CODE, code_bits)
    writer.close()

    print(f"Compressed: {input_file} -> {output_file}")


# ============================================================================
# DECOMPRESSION (Decoder with own LRU Tracker)
# ============================================================================

def decompress(input_file, output_file):
    """
    Decompress a file using symmetric LZW-LRU.

    The decoder maintains its own LRU tracker and performs IDENTICAL dictionary
    operations as the encoder. No eviction signals are needed because both sides:
      1. Add the same entries at the same time (deferred addition formula)
      2. Perform the same LRU.use() operations in the same order
      3. Evict the same entry when the dictionary is full

    Algorithm per step j (j ≥ 2):
      1. Read codeword, decode to string s_j
      2. Write s_j to output
      3. LRU.use(s_j) if tracked
      4. Add entry = prev_output + s_j[0]  (identical to encoder's step)
      5. LRU.use(entry)
      6. Update prev_output = s_j

    The codeword==next_code special case from standard LZW is ELIMINATED because
    the decoder always has every entry the encoder references.
    """
    reader = BitReader(input_file)

    # ── Read header ──
    min_bits = reader.read(8)
    max_bits = reader.read(8)
    alphabet_size = reader.read(16)
    alphabet = [chr(reader.read(8)) for _ in range(alphabet_size)]

    # ── Initialize dictionary (must match encoder) ──
    fwd = {char: i for i, char in enumerate(alphabet)}
    rev = {i: char for i, char in enumerate(alphabet)}

    EOF_CODE = alphabet_size
    max_size = 1 << max_bits
    max_code = max_size
    next_code = alphabet_size + 1

    code_bits = min_bits
    threshold = 1 << code_bits

    # Decoder's own LRU tracker — mirrors encoder's exactly
    lru = LRUTracker()

    # ── Read first code ──
    codeword = reader.read(code_bits)
    if codeword is None:
        raise ValueError("Corrupted file: unexpected end of file")

    if codeword == EOF_CODE:
        reader.close()
        open(output_file, 'wb').close()
        return

    if codeword not in rev:
        raise ValueError(f"Invalid first codeword: {codeword}")

    prev_output = rev[codeword]

    with open(output_file, 'wb') as out:
        out.write(prev_output.encode('latin-1'))

        # Mirror encoder's LRU.use() for first output
        if lru.contains(prev_output):
            lru.use(prev_output)

        # ── Main decompression loop ──
        while True:
            codeword = reader.read(code_bits)
            if codeword is None:
                raise ValueError("Corrupted file: unexpected end of file")
            if codeword == EOF_CODE:
                break

            # Decode codeword
            if codeword not in rev:
                raise ValueError(f"Invalid codeword: {codeword} (not in dictionary)")
            current = rev[codeword]

            # Write decoded output
            out.write(current.encode('latin-1'))

            # Mirror encoder's LRU.use(current)
            if lru.contains(current):
                lru.use(current)

            # ── Deferred addition (identical to encoder) ──
            entry = prev_output + current[0]
            next_code, code_bits, threshold = dict_add_entry(
                fwd, rev, entry, next_code, max_code,
                lru, code_bits, max_bits, threshold
            )

            prev_output = current

    reader.close()
    print(f"Decompressed: {input_file} -> {output_file}")


# ============================================================================
# COMMAND-LINE INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='LZW compression with symmetric LRU eviction (no bitstream signals)')
    sub = parser.add_subparsers(dest='mode', required=True)

    c = sub.add_parser('compress', help='Compress a file')
    c.add_argument('input', help='Input file to compress')
    c.add_argument('output', help='Output compressed file')
    c.add_argument('--alphabet', required=True, choices=list(ALPHABETS.keys()),
                   help='Alphabet to use for encoding')
    c.add_argument('--min-bits', type=int, default=9,
                   help='Minimum bit width for codes (default: 9)')
    c.add_argument('--max-bits', type=int, default=16,
                   help='Maximum bit width for codes (default: 16)')

    d = sub.add_parser('decompress', help='Decompress a file')
    d.add_argument('input', help='Input compressed file')
    d.add_argument('output', help='Output decompressed file')

    args = parser.parse_args()

    try:
        if args.mode == 'compress':
            compress(args.input, args.output, args.alphabet, args.min_bits, args.max_bits)
        else:
            decompress(args.input, args.output)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
