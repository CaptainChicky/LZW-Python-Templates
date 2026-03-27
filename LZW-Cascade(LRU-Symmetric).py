#!/usr/bin/env python3
"""
LZW Compression with Symmetric LRU (Deferred Addition) - WITH CASCADE DELETION

When evicting an entry, also evict all its orphaned descendants.
This ensures no dictionary slots are wasted on unreachable entries.
Amortized O(n) — each entry created once, deleted once.
"""

import sys
import argparse
from typing import TypeVar, Generic, Optional, Dict

ALPHABETS = {
    'ascii': [chr(i) for i in range(128)],
    'extendedascii': [chr(i) for i in range(256)],
    'ab': ['a', 'b']
}

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


K = TypeVar('K')

class LRUTracker(Generic[K]):
    __slots__ = ('map', 'head', 'tail')

    class Node:
        __slots__ = ('key', 'prev', 'next')
        def __init__(self, key):
            self.key = key
            self.prev = None
            self.next = None

    def __init__(self):
        self.map = {}
        self.head = self.Node(None)
        self.tail = self.Node(None)
        self.head.next = self.tail
        self.tail.prev = self.head

    def use(self, key):
        node = self.map.get(key)
        if node is not None:
            self._remove_node(node)
            self._add_to_front(node)
        else:
            node = self.Node(key)
            self.map[key] = node
            self._add_to_front(node)

    def find_lru(self):
        if self.tail.prev == self.head:
            return None
        return self.tail.prev.key

    def remove(self, key):
        node = self.map.pop(key, None)
        if node is not None:
            self._remove_node(node)

    def contains(self, key):
        return key in self.map

    def _add_to_front(self, node):
        node.next = self.head.next
        node.prev = self.head
        self.head.next.prev = node
        self.head.next = node

    def _remove_node(self, node):
        node.prev.next = node.next
        node.next.prev = node.prev


def cascade_evict(entry, fwd, rev, lru, children_of, free_codes):
    """
    Evict entry and all its orphaned descendants recursively.
    Freed code slots go into free_codes for reuse.
    Uses iterative stack to avoid recursion depth issues.
    """
    stack = [entry]
    while stack:
        e = stack.pop()
        if e not in fwd:
            continue  # already evicted by a prior iteration
        
        # Push children onto stack before evicting this node
        if e in children_of:
            for child in children_of.pop(e):
                if child in fwd:
                    stack.append(child)

        # Remove parent's reference to this entry
        parent = e[:-1]
        if parent in children_of:
            children_of[parent].discard(e)
            if not children_of[parent]:
                del children_of[parent]

        # Evict this entry
        code = fwd.pop(e)
        del rev[code]
        lru.remove(e)
        free_codes.append(code)


def dict_add_entry(fwd, rev, entry, next_code, max_code, lru, code_bits, max_bits, threshold,
                   children_of, free_codes):
    if entry in fwd:
        return next_code, code_bits, threshold

    if free_codes:
        # Reuse a freed slot from cascade deletion
        reuse_code = free_codes.pop()
        fwd[entry] = reuse_code
        rev[reuse_code] = entry
        lru.use(entry)
        # Track parent-child relationship
        parent = entry[:-1]
        if len(parent) > 1:  # only track multi-char parents (not alphabet entries)
            children_of.setdefault(parent, set()).add(entry)
    elif next_code < max_code:
        fwd[entry] = next_code
        rev[next_code] = entry
        lru.use(entry)
        # Track parent-child relationship
        parent = entry[:-1]
        if len(parent) > 1:
            children_of.setdefault(parent, set()).add(entry)
        next_code += 1
        if next_code >= threshold and code_bits < max_bits:
            code_bits += 1
            threshold <<= 1
    else:
        lru_entry = lru.find_lru()
        if lru_entry is not None:
            # Cascade evict: remove lru_entry and all its orphaned descendants
            cascade_evict(lru_entry, fwd, rev, lru, children_of, free_codes)

            # Now use one of the freed codes for the new entry
            reuse_code = free_codes.pop()
            fwd[entry] = reuse_code
            rev[reuse_code] = entry
            lru.use(entry)
            # Track parent-child relationship
            parent = entry[:-1]
            if len(parent) > 1:
                children_of.setdefault(parent, set()).add(entry)

    return next_code, code_bits, threshold


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
    children_of = {}   # parent_entry -> set of child entries
    free_codes = []     # freed code slots from cascade deletion

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


def decompress(input_file, output_file):
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