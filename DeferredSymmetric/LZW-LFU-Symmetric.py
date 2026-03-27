#!/usr/bin/env python3
"""
LZW Compression with Symmetric LFU (Deferred Addition)

Implements LZW compression with LFU (Least Frequently Used) eviction where
the decoder maintains its own LFU tracker that perfectly mirrors the encoder's.
No eviction signals are sent in the bitstream.

Same deferred addition principle as the symmetric LRU version, but evicts by
frequency (with LRU tie-breaking) instead of pure recency.

Bitstream: pure codewords + EOF. No EVICT_SIGNAL, no metadata.

Usage:
    Compress:   python3 LZW-LFU-Symmetric.py compress input.txt output.lzw --alphabet ascii
    Decompress: python3 LZW-LFU-Symmetric.py decompress input.lzw output.txt
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
# BIT-LEVEL I/O
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
# LFU TRACKER
# ============================================================================

K = TypeVar('K')

class LFUTracker(Generic[K]):
    """O(1) LFU tracker with frequency buckets + LRU tie-breaking."""
    __slots__ = ('key_to_node', 'freq_to_list', 'min_freq')

    class Node:
        __slots__ = ('key', 'freq', 'prev', 'next')
        def __init__(self, key, freq):
            self.key = key
            self.freq = freq
            self.prev = None
            self.next = None

    class FreqList:
        __slots__ = ('outer_class', 'head', 'tail')
        def __init__(self, outer_class):
            self.outer_class = outer_class
            self.head = outer_class.Node(None, 0)
            self.tail = outer_class.Node(None, 0)
            self.head.next = self.tail
            self.tail.prev = self.head

        def add_to_front(self, node):
            node.next = self.head.next
            node.prev = self.head
            self.head.next.prev = node
            self.head.next = node

        def remove(self, node):
            node.prev.next = node.next
            node.next.prev = node.prev

        def is_empty(self):
            return self.head.next == self.tail

        def get_last(self):
            if self.tail.prev == self.head:
                return None
            return self.tail.prev

    def __init__(self):
        self.key_to_node: Dict[K, 'LFUTracker.Node'] = {}
        self.freq_to_list: Dict[int, 'LFUTracker.FreqList'] = {}
        self.min_freq: int = 0

    def use(self, key: K) -> None:
        node = self.key_to_node.get(key)
        if node is None:
            node = self.Node(key, 1)
            self.key_to_node[key] = node
            if 1 not in self.freq_to_list:
                self.freq_to_list[1] = self.FreqList(self.__class__)
            self.freq_to_list[1].add_to_front(node)
            self.min_freq = 1
        else:
            old_freq = node.freq
            old_list = self.freq_to_list[old_freq]
            old_list.remove(node)
            if old_freq == self.min_freq and old_list.is_empty():
                self.min_freq = old_freq + 1
            node.freq += 1
            if node.freq not in self.freq_to_list:
                self.freq_to_list[node.freq] = self.FreqList(self.__class__)
            self.freq_to_list[node.freq].add_to_front(node)

    def find_lfu(self) -> Optional[K]:
        min_list = self.freq_to_list.get(self.min_freq)
        if min_list is None or min_list.is_empty():
            return None
        return min_list.get_last().key

    def remove(self, key: K) -> None:
        node = self.key_to_node.pop(key, None)
        if node is not None:
            self.freq_to_list[node.freq].remove(node)

    def contains(self, key: K) -> bool:
        return key in self.key_to_node


# ============================================================================
# SHARED DICTIONARY MANAGEMENT
# ============================================================================
# Identical to LRU-Symmetric's dict_add_entry but calls find_lfu instead of find_lru.

def dict_add_entry(fwd, rev, entry, next_code, max_code, lfu, code_bits, max_bits, threshold):
    """
    Add a new entry to the bidirectional dictionary.
    If full, evict LFU entry (with LRU tie-breaking) and reuse its code.
    Both encoder and decoder must call this identically to stay in sync.
    """
    if entry in fwd:
        return next_code, code_bits, threshold

    if next_code < max_code:
        fwd[entry] = next_code
        rev[next_code] = entry
        lfu.use(entry)
        next_code += 1
        if next_code >= threshold and code_bits < max_bits:
            code_bits += 1
            threshold <<= 1
    else:
        # Evict least frequently used (LRU tie-breaking) instead of least recently used
        lfu_entry = lfu.find_lfu()
        if lfu_entry is not None:
            lfu_code = fwd[lfu_entry]
            del fwd[lfu_entry]
            del rev[lfu_code]
            lfu.remove(lfu_entry)
            fwd[entry] = lfu_code
            rev[lfu_code] = entry
            lfu.use(entry)

    return next_code, code_bits, threshold


# ============================================================================
# COMPRESSION
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

    fwd = {char: i for i, char in enumerate(alphabet)}
    rev = {i: char for i, char in enumerate(alphabet)}

    EOF_CODE = len(alphabet)
    max_size = 1 << max_bits
    max_code = max_size
    next_code = len(alphabet) + 1

    code_bits = min_bits
    threshold = 1 << code_bits

    lfu = LFUTracker()

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
        prev_output = None
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

                if lfu.contains(current):
                    lfu.use(current)

                if prev_output is not None:
                    entry = prev_output + current[0]
                    next_code, code_bits, threshold = dict_add_entry(
                        fwd, rev, entry, next_code, max_code,
                        lfu, code_bits, max_bits, threshold
                    )

                prev_output = current
                current = char

    writer.write(fwd[current], code_bits)

    if lfu.contains(current):
        lfu.use(current)

    if prev_output is not None:
        entry = prev_output + current[0]
        next_code, code_bits, threshold = dict_add_entry(
            fwd, rev, entry, next_code, max_code,
            lfu, code_bits, max_bits, threshold
        )

    writer.write(EOF_CODE, code_bits)
    writer.close()
    print(f"Compressed: {input_file} -> {output_file}")


# ============================================================================
# DECOMPRESSION
# ============================================================================

def decompress(input_file, output_file):
    """Decoder mirrors encoder's LFU state exactly via shared dict_add_entry."""
    reader = BitReader(input_file)

    min_bits = reader.read(8)
    max_bits = reader.read(8)
    alphabet_size = reader.read(16)
    alphabet = [chr(reader.read(8)) for _ in range(alphabet_size)]

    fwd = {char: i for i, char in enumerate(alphabet)}
    rev = {i: char for i, char in enumerate(alphabet)}

    EOF_CODE = alphabet_size
    max_size = 1 << max_bits
    max_code = max_size
    next_code = alphabet_size + 1

    code_bits = min_bits
    threshold = 1 << code_bits

    lfu = LFUTracker()

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

        if lfu.contains(prev_output):
            lfu.use(prev_output)

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

            if lfu.contains(current):
                lfu.use(current)

            if prev_output is not None:
                entry = prev_output + current[0]
                next_code, code_bits, threshold = dict_add_entry(
                    fwd, rev, entry, next_code, max_code,
                    lfu, code_bits, max_bits, threshold
                )

            prev_output = current

    reader.close()
    print(f"Decompressed: {input_file} -> {output_file}")


# ============================================================================
# COMMAND-LINE INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='LZW compression with symmetric LFU eviction (no bitstream signals)')
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