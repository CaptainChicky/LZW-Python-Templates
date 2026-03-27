#!/usr/bin/env python3
"""
LZW Compression with Symmetric LRU + Cascade Deletion (Deferred Addition)
I chose Symmetric LRU as an example, and the same cascade strat can be applied to other stuff like LFU.

Same symmetric/deferred-addition approach as LRU-Symmetric, but when evicting
an entry, also evicts all its orphaned descendants. This ensures no dictionary
slots are wasted on entries whose prefix no longer exists (and thus can never
be matched). Freed slots are collected and reused before allocating new codes.

Amortized O(n) since each entry is created once and deleted at most once.

Bitstream: pure codewords + EOF. No EVICT_SIGNAL, no metadata.

Usage:
    Compress:   python3 LZW-Cascade(LRU-Symmetric).py compress input.txt output.lzw --alphabet ascii
    Decompress: python3 LZW-Cascade(LRU-Symmetric).py decompress input.lzw output.txt
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
# LRU TRACKER
# ============================================================================

K = TypeVar('K')

class LRUTracker(Generic[K]):
    """O(1) LRU tracker using doubly-linked list + HashMap."""
    __slots__ = ('map', 'head', 'tail')

    class Node:
        __slots__ = ('key', 'prev', 'next')
        def __init__(self, key: Optional[K]) -> None:
            self.key: Optional[K] = key
            self.prev: Optional['LRUTracker.Node'] = None
            self.next: Optional['LRUTracker.Node'] = None

    def __init__(self) -> None:
        self.map: Dict[K, 'LRUTracker.Node'] = {}
        self.head: LRUTracker.Node = self.Node(None)
        self.tail: LRUTracker.Node = self.Node(None)
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

    def _add_to_front(self, node: 'LRUTracker.Node') -> None:
        node.next = self.head.next
        node.prev = self.head
        self.head.next.prev = node  # type: ignore
        self.head.next = node

    def _remove_node(self, node: 'LRUTracker.Node') -> None:
        node.prev.next = node.next  # type: ignore
        node.next.prev = node.prev  # type: ignore


# ============================================================================
# CASCADE EVICTION
# ============================================================================

def cascade_evict(entry, fwd, rev, lru, children_of, free_codes):
    """
    Evict entry and all its orphaned descendants, freeing their code slots.

    In standard LRU eviction, removing "abc" leaves "abcd", "abcde" etc. in
    the dictionary even though they can never be matched (their prefix is gone).
    Cascade deletion reclaims those wasted slots.

    Uses an iterative stack to avoid recursion depth issues on long chains.
    """
    stack = [entry]
    while stack:
        e = stack.pop()
        if e not in fwd:
            continue  # already evicted by a prior iteration

        # Push children onto stack before evicting this node
        if e in children_of:
            for child in sorted(children_of.pop(e)):
                if child in fwd:
                    stack.append(child)

        # Remove parent's reference to this entry
        parent = e[:-1]
        if parent in children_of:
            children_of[parent].discard(e)
            if not children_of[parent]:
                del children_of[parent]

        # Evict this entry and collect freed code for reuse
        code = fwd.pop(e)
        del rev[code]
        lru.remove(e)
        free_codes.append(code)


# ============================================================================
# SHARED DICTIONARY MANAGEMENT
# ============================================================================
# Extends LRU-Symmetric's dict_add_entry with cascade deletion and code reuse.
# Three paths: (1) reuse a freed slot, (2) allocate next_code, (3) cascade evict LRU.

def dict_add_entry(fwd, rev, entry, next_code, max_code, lru, code_bits, max_bits, threshold,
                   children_of, free_codes):
    """
    Add a new entry to the bidirectional dictionary.
    Both encoder and decoder must call this identically to stay in sync.
    """
    if entry in fwd:
        return next_code, code_bits, threshold

    if free_codes:
        # Reuse a freed slot from a previous cascade deletion
        reuse_code = free_codes.pop()
        fwd[entry] = reuse_code
        rev[reuse_code] = entry
        lru.use(entry)
        _track_parent(entry, children_of)
    elif next_code < max_code:
        fwd[entry] = next_code
        rev[next_code] = entry
        lru.use(entry)
        _track_parent(entry, children_of)
        next_code += 1
        if next_code >= threshold and code_bits < max_bits:
            code_bits += 1
            threshold <<= 1
    else:
        # Dictionary full, no free slots -- cascade evict LRU and reuse a freed code
        lru_entry = lru.find_lru()
        if lru_entry is not None:
            cascade_evict(lru_entry, fwd, rev, lru, children_of, free_codes)
            reuse_code = free_codes.pop()
            fwd[entry] = reuse_code
            rev[reuse_code] = entry
            lru.use(entry)
            _track_parent(entry, children_of)

    return next_code, code_bits, threshold


def _track_parent(entry, children_of):
    """Register entry as a child of its prefix (parent) for cascade tracking.
    Only tracks multi-char parents since single-char alphabet entries are permanent."""
    parent = entry[:-1]
    if len(parent) > 1:
        children_of.setdefault(parent, set()).add(entry)


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

    lru = LRUTracker()
    children_of = {}  # parent entry -> set of child entries (for cascade deletion)
    free_codes = []   # code slots freed by cascade deletion, available for reuse

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

                if lru.contains(current):
                    lru.use(current)

                # Deferred addition (same as LRU-Symmetric)
                if prev_output is not None:
                    entry = prev_output + current[0]
                    next_code, code_bits, threshold = dict_add_entry(
                        fwd, rev, entry, next_code, max_code,
                        lru, code_bits, max_bits, threshold,
                        children_of, free_codes
                    )

                prev_output = current
                current = char

    writer.write(fwd[current], code_bits)

    if lru.contains(current):
        lru.use(current)

    if prev_output is not None:
        entry = prev_output + current[0]
        next_code, code_bits, threshold = dict_add_entry(
            fwd, rev, entry, next_code, max_code,
            lru, code_bits, max_bits, threshold,
            children_of, free_codes
        )

    writer.write(EOF_CODE, code_bits)
    writer.close()
    print(f"Compressed: {input_file} -> {output_file}")


# ============================================================================
# DECOMPRESSION
# ============================================================================

def decompress(input_file, output_file):
    """Decoder mirrors encoder exactly, including cascade eviction and code reuse."""
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

    lru = LRUTracker()
    children_of = {}
    free_codes = []

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

            entry = prev_output + current[0]
            next_code, code_bits, threshold = dict_add_entry(
                fwd, rev, entry, next_code, max_code,
                lru, code_bits, max_bits, threshold,
                children_of, free_codes
            )

            prev_output = current

    reader.close()
    print(f"Decompressed: {input_file} -> {output_file}")


# ============================================================================
# COMMAND-LINE INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='LZW compression with symmetric LRU + cascade deletion (no bitstream signals)')
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