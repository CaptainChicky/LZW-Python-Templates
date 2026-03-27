#!/usr/bin/env python3
"""
LZW Compression with Symmetric LRU (Deferred Addition)

Implements LZW compression with LRU eviction where the decoder maintains its
own LRU tracker that perfectly mirrors the encoder's. NO eviction signals are
sent -- the bitstream contains only LZW codewords and an EOF marker.

The key insight is "deferred addition": both sides compute new entries as
    entry = prev_output + current_output[0]
and add them at the same logical point (after decode/output). The encoder
defers its addition by one step to match the decoder's natural timing.

This doesn't change WHICH entries are added, only WHEN. Entry from step j-1
becomes available at step j instead of step j-1. For large files this has
negligible impact on compression ratio.

Because both sides perform identical operations in identical order, their
LRU states are always identical. When the dictionary is full, both evict the
same entry. BONUS: the codeword==next_code special case from standard LZW is
eliminated, since the decoder always has every entry the encoder references.

Bitstream format: [header] [codewords...] [EOF_CODE]
No EVICT_SIGNAL. No eviction metadata. Just codewords.

Usage:
    Compress:   python3 LZW-LRU-Symmetric.py compress input.txt output.lzw --alphabet ascii
    Decompress: python3 LZW-LRU-Symmetric.py decompress input.lzw output.txt
"""

import sys
import argparse
from typing import TypeVar, Generic, Optional, Dict

ALPHABETS = {
    'ascii': [chr(i) for i in range(128)],
    'extendedascii': [chr(i) for i in range(256)],
    'ab': ['a', 'b']
}


# ============================================================================
# BIT-LEVEL I/O CLASSES
# ============================================================================

class BitWriter:
    def __init__(self, filename):
        self.file = open(filename, 'wb')
        self.buffer = 0
        self.n_bits = 0

    def write(self, value, num_bits):
        self.buffer = (self.buffer << num_bits) | value
        self.n_bits += num_bits
        while self.n_bits >= 8:
            self.n_bits -= 8
            byte = self.buffer >> self.n_bits
            self.file.write(bytes([byte]))
            self.buffer &= (1 << self.n_bits) - 1

    def close(self):
        if self.n_bits > 0:
            byte = self.buffer << (8 - self.n_bits)
            self.file.write(bytes([byte]))
        self.file.close()


class BitReader:
    def __init__(self, filename):
        self.file = open(filename, 'rb')
        self.buffer = 0
        self.n_bits = 0

    def read(self, num_bits):
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
    """O(1) LRU tracker using doubly-linked list + HashMap."""
    __slots__ = ('map', 'head', 'tail')

    class Node:
        __slots__ = ('key', 'prev', 'next')
        def __init__(self, key: Optional[K]) -> None:
            self.key = key
            self.prev = None
            self.next = None

    def __init__(self) -> None:
        self.map: Dict[K, 'LRUTracker.Node'] = {}
        self.head = self.Node(None)
        self.tail = self.Node(None)
        self.head.next = self.tail
        self.tail.prev = self.head

    def use(self, key: K) -> None:
        node = self.map.get(key)
        if node is not None:
            self._remove_node(node)
            self._add_to_front(node)
        else:
            node = self.Node(key)
            self.map[key] = node
            self._add_to_front(node)

    def find_lru(self) -> Optional[K]:
        if self.tail.prev == self.head:
            return None
        return self.tail.prev.key

    def remove(self, key: K) -> None:
        node = self.map.pop(key, None)
        if node is not None:
            self._remove_node(node)

    def contains(self, key: K) -> bool:
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
# This is the heart of the symmetric approach: by running the same code,
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
        (next_code, code_bits, threshold) -- updated values
    """
    # Skip if entry already exists. Deferred addition can produce duplicate
    # entries when the same prev+current[0] combination arises multiple times.
    if entry in fwd:
        return next_code, code_bits, threshold

    if next_code < max_code:
        # Dictionary not full: add at next available code
        fwd[entry] = next_code
        rev[next_code] = entry
        lru.use(entry)
        next_code += 1
        # Check bit width AFTER incrementing so returned code_bits is immediately
        # correct for the next read/write (avoids EOF desync)
        if next_code >= threshold and code_bits < max_bits:
            code_bits += 1
            threshold <<= 1
    else:
        # Dictionary full: evict LRU entry, reuse its code
        lru_entry = lru.find_lru()
        if lru_entry is not None:
            lru_code = fwd[lru_entry]
            del fwd[lru_entry]
            del rev[lru_code]
            lru.remove(lru_entry)
            fwd[entry] = lru_code
            rev[lru_code] = entry
            lru.use(entry)

    return next_code, code_bits, threshold


# ============================================================================
# COMPRESSION (Encoder with Deferred Addition)
# ============================================================================

def compress(input_file, output_file, alphabet_name, min_bits=9, max_bits=16):
    alphabet = ALPHABETS[alphabet_name]
    valid_chars = set(alphabet)

    writer = BitWriter(output_file)
    writer.write(min_bits, 8)
    writer.write(max_bits, 8)
    writer.write(len(alphabet), 16)
    for char in alphabet:
        writer.write(ord(char), 8)

    # Bidirectional dictionary: fwd (string->code) for encoding,
    # rev (code->string) kept in sync for dict_add_entry compatibility
    fwd = {char: i for i, char in enumerate(alphabet)}
    rev = {i: char for i, char in enumerate(alphabet)}

    # No EVICT_SIGNAL reserved -- all codes available for dictionary entries
    EOF_CODE = len(alphabet)
    max_size = 1 << max_bits
    max_code = max_size
    next_code = len(alphabet) + 1

    code_bits = min_bits
    threshold = 1 << code_bits

    lru = LRUTracker()

    with open(input_file, 'rb') as f:
        first_byte = f.read(1)
        if not first_byte:
            writer.write(EOF_CODE, min_bits)
            writer.close()
            return

        first_char = chr(first_byte[0])
        if first_char not in valid_chars:
            raise ValueError(f"Byte {first_byte[0]} at position 0 not in alphabet")

        current = first_char
        prev_output = None  # Tracks previous step's output for deferred entry computation
        pos = 1

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
                current = combined
            else:
                writer.write(fwd[current], code_bits)

                if lru.contains(current):
                    lru.use(current)

                # Deferred addition: add entry from the PREVIOUS step
                # entry = prev_output + current[0], matching decoder's formula.
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

    # Output final match and its deferred entry
    writer.write(fwd[current], code_bits)

    if lru.contains(current):
        lru.use(current)

    if prev_output is not None:
        entry = prev_output + current[0]
        next_code, code_bits, threshold = dict_add_entry(
            fwd, rev, entry, next_code, max_code,
            lru, code_bits, max_bits, threshold
        )

    writer.write(EOF_CODE, code_bits)
    writer.close()

    print(f"Compressed: {input_file} -> {output_file}")


# ============================================================================
# DECOMPRESSION (Decoder with its own LRU Tracker)
# ============================================================================

def decompress(input_file, output_file):
    """
    Decoder maintains its own LRU tracker and performs IDENTICAL dictionary
    operations as the encoder via dict_add_entry. No eviction signals needed.

    The codeword==next_code special case from standard LZW is eliminated
    because the decoder always has every entry the encoder references.
    """
    reader = BitReader(input_file)

    min_bits = reader.read(8)
    max_bits = reader.read(8)
    alphabet_size = reader.read(16)
    alphabet = [chr(reader.read(8)) for _ in range(alphabet_size)]

    # Bidirectional dictionary (must match encoder's initial state)
    fwd = {char: i for i, char in enumerate(alphabet)}
    rev = {i: char for i, char in enumerate(alphabet)}

    # No EVICT_SIGNAL -- all codes available for dictionary entries
    EOF_CODE = alphabet_size
    max_size = 1 << max_bits
    max_code = max_size
    next_code = alphabet_size + 1

    code_bits = min_bits
    threshold = 1 << code_bits

    # Decoder's own LRU tracker -- mirrors encoder's exactly
    lru = LRUTracker()

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

        if lru.contains(prev_output):
            lru.use(prev_output)

        while True:
            codeword = reader.read(code_bits)
            if codeword is None:
                raise ValueError("Corrupted file: unexpected end of file")
            if codeword == EOF_CODE:
                break

            if codeword not in rev:
                raise ValueError(f"Invalid codeword: {codeword} (not in dictionary)")
            current = rev[codeword]

            out.write(current.encode('latin-1'))

            if lru.contains(current):
                lru.use(current)

            # Same deferred addition as encoder: entry = prev_output + current[0]
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

    c = sub.add_parser('compress')
    c.add_argument('input')
    c.add_argument('output')
    c.add_argument('--alphabet', required=True, choices=list(ALPHABETS.keys()))
    c.add_argument('--min-bits', type=int, default=9)
    c.add_argument('--max-bits', type=int, default=16)

    d = sub.add_parser('decompress')
    d.add_argument('input')
    d.add_argument('output')

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