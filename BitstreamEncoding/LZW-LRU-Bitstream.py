#!/usr/bin/env python3
"""
LZW Compression Tool (Optimization: Output History + Offset/Suffix with O(1) HashMap)

Implements LZW compression with LRU (Least Recently Used) eviction policy.
This is optimized to send compact EVICT_SIGNAL using output history reference.

Implements LZW compression with LRU eviction, using an OPTIMIZED signaling strategy:
- Only sends EVICT_SIGNAL when encoder evicts code C and immediately uses C (~10-30% of evictions)
- Instead of sending full entry, uses output history to send compact offset+suffix
- This reduces EVICT_SIGNAL size by ~57% (34 bits vs 123 bits for 10-char entry)

How It Works:
1. Encoder tracks all evicted codes in a dictionary (like Optimization 1)
2. Encoder maintains circular buffer of last 255 outputs with HashMap for O(1) lookup
3. When about to output a code that was recently evicted:
   a. Find prefix in output history using O(1) HashMap lookup
   b. Send compact [offset][suffix] (2 bytes) instead of full entry
   c. If prefix not in history, fall back to full entry format
4. Decoder maintains same output history, reconstructs entry from offset+suffix
5. Both stay synchronized through LRU tracking and output history mirroring

Data Structure:
- Doubly-linked list + HashMap for O(1) LRU operations
- Sentinel head/tail nodes eliminate edge cases
- Output history: Circular buffer (last 255 outputs) with HashMap (string -> index)

EVICT_SIGNAL Format (Compact, used ~95% of time):
- [EVICT_SIGNAL][code][offset][suffix][code_again]
- Total: code_bits + code_bits + 8 + 8 + code_bits bits
- Example (9-bit codes): 9+9+8+8+9 = 43 bits (vs 123 bits in Opt-1!)

EVICT_SIGNAL Format (Fallback, when prefix not in recent history):
- [EVICT_SIGNAL][code][0][entry_length][char1]...[charN][code_again]
- offset=0 signals "full entry follows" (0 is never a valid offset)
- Example (9-bit codes, 10-char entry): 9+9+8+16+80+9 = 131 bits

Usage:
    Compress:   python3 LZW-LRU-Bitstream.py compress input.txt output.lzw --alphabet ascii
    Decompress: python3 LZW-LRU-Bitstream.py decompress input.lzw output.txt
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

K = TypeVar('K')  # Key type (can be str, int, or any hashable type)

class LRUTracker(Generic[K]):
    """
    O(1) LRU tracker using doubly-linked list + HashMap.
    Works with any hashable key type (strings, integers, etc).

    Type-safe generic class: LRUTracker[str] for strings, LRUTracker[int] for ints.
    """
    __slots__ = ('map', 'head', 'tail')

    class Node:
        __slots__ = ('key', 'prev', 'next')

        def __init__(self, key: Optional[K]) -> None:
            self.key: Optional[K] = key
            self.prev: Optional['LRUTracker.Node'] = None
            self.next: Optional['LRUTracker.Node'] = None

    def __init__(self) -> None:
        self.map: Dict[K, 'LRUTracker.Node'] = {}
        # Sentinel nodes eliminate edge cases in list operations
        self.head: LRUTracker.Node = self.Node(None)
        self.tail: LRUTracker.Node = self.Node(None)
        self.head.next = self.tail
        self.tail.prev = self.head

    def use(self, key: K) -> None:
        """Mark key as recently used. Adds key if not present."""
        node = self.map.get(key)
        if node is not None:
            # Key exists so move to front (most recently used)
            self._remove_node(node)
            self._add_to_front(node)
        else:
            # New key so add to front
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

    def _add_to_front(self, node: 'LRUTracker.Node') -> None:
        """Add node after head (most recently used position)."""
        node.next = self.head.next
        node.prev = self.head
        self.head.next.prev = node  # type: ignore
        self.head.next = node

    def _remove_node(self, node: 'LRUTracker.Node') -> None:
        """Remove node from list (maintains links)."""
        node.prev.next = node.next  # type: ignore
        node.next.prev = node.prev  # type: ignore

# ============================================================================
# LZW COMPRESSION WITH OPTIMIZATION (Output History + O(1) HashMap)
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

    dictionary = {char: i for i, char in enumerate(alphabet)}

    # Reserve codes:
    # - len(alphabet): EOF marker
    # - len(alphabet)+1 to max_size-2: dictionary entries
    # - max_size-1: EVICT_SIGNAL (synchronization code for evict-then-use pattern)
    EOF_CODE = len(alphabet)
    max_size = 1 << max_bits
    EVICT_SIGNAL = max_size - 1
    next_code = len(alphabet) + 1

    code_bits = min_bits
    threshold = 1 << code_bits

    # LRU tracker for dictionary entries only (alphabet entries are never evicted)
    lru_tracker = LRUTracker()

    # Track evicted codes pending sync with decoder
    # Key: evicted code, Value: (full_entry, prefix_at_eviction_time)
    # When encoder outputs a recently-evicted code, decoder won't know the new value,
    # so we send EVICT_SIGNAL to synchronize
    evicted_codes = {}

    # Output history: circular buffer of last 255 outputs with O(1) HashMap lookup
    # Enables compact offset+suffix encoding in EVICT_SIGNAL instead of full entry
    OUTPUT_HISTORY_SIZE = 255
    output_history = []
    history_start_idx = 0         # Absolute position of first element in buffer
    string_to_idx = {}            # Maps string -> absolute index for O(1) lookup

    with open(input_file, 'rb') as f:
        first_byte = f.read(1)

        if not first_byte:
            writer.write(EOF_CODE, min_bits)
            writer.close()
            return

        first_char = chr(first_byte[0])

        if first_char not in valid_chars:
            raise ValueError(f"Byte value {first_byte[0]} at position 0 not in alphabet")

        current = first_char
        pos = 1

        while True:
            byte_data = f.read(1)
            if not byte_data:
                break

            char = chr(byte_data[0])

            if char not in valid_chars:
                raise ValueError(f"Byte value {byte_data[0]} at position {pos} not in alphabet")
            pos += 1

            combined = current + char

            if combined in dictionary:
                # Don't update LRU yet; only update when we actually output the code
                current = combined
            else:
                output_code = dictionary[current]

                # Check if this code was evicted and is being reused (evict-then-use pattern)
                if output_code in evicted_codes:
                    entry, prefix = evicted_codes[output_code]

                    suffix = entry[len(prefix):]
                    if len(suffix) != 1:
                        raise ValueError(f"Logic error: suffix should be 1 char, got {len(suffix)}")

                    # Try O(1) HashMap lookup for prefix in output history
                    offset = None
                    if prefix in string_to_idx:
                        prefix_global_idx = string_to_idx[prefix]
                        if prefix_global_idx >= history_start_idx:
                            current_end_idx = history_start_idx + len(output_history) - 1
                            offset = current_end_idx - prefix_global_idx + 1

                    if offset is not None:
                        if offset > 255:
                            raise ValueError(f"Bug in circular buffer: offset {offset} exceeds 255! "
                                            f"history_size={len(output_history)}, prefix_idx={prefix_global_idx}, "
                                            f"history_start={history_start_idx}")
                        # Compact EVICT_SIGNAL: [EVICT_SIGNAL][code][offset][suffix]
                        writer.write(EVICT_SIGNAL, code_bits)
                        writer.write(output_code, code_bits)
                        writer.write(offset, 8)
                        writer.write(ord(suffix), 8)
                    else:
                        # Fallback: offset=0 signals full entry follows
                        # Format: [EVICT_SIGNAL][code][0][entry_length][char1]...[charN]
                        writer.write(EVICT_SIGNAL, code_bits)
                        writer.write(output_code, code_bits)
                        writer.write(0, 8)
                        writer.write(len(entry), 16)
                        for c in entry:
                            writer.write(ord(c), 8)

                    del evicted_codes[output_code]

                # Output code for current phrase (repeated after EVICT_SIGNAL if applicable)
                writer.write(output_code, code_bits)

                # Add current output to history, maintaining circular buffer
                current_global_idx = history_start_idx + len(output_history)
                output_history.append(current)
                string_to_idx[current] = current_global_idx

                if len(output_history) > OUTPUT_HISTORY_SIZE:
                    output_history.pop(0)
                    history_start_idx += 1

                # Update LRU only for tracked entries (not single-char alphabet entries)
                if lru_tracker.contains(current):
                    lru_tracker.use(current)

                if next_code < EVICT_SIGNAL:
                    if next_code >= threshold and code_bits < max_bits:
                        code_bits += 1
                        threshold <<= 1

                    dictionary[combined] = next_code
                    lru_tracker.use(combined)
                    next_code += 1
                else:
                    # Dictionary full: evict LRU entry and reuse its code
                    lru_entry = lru_tracker.find_lru()
                    if lru_entry is not None:
                        lru_code = dictionary[lru_entry]

                        del dictionary[lru_entry]
                        lru_tracker.remove(lru_entry)

                        dictionary[combined] = lru_code
                        lru_tracker.use(combined)

                        # Track eviction with full entry and prefix for offset+suffix encoding
                        evicted_codes[lru_code] = (combined, current)

                current = char

    # Write final phrase (same evict-then-use check as main loop)
    final_code = dictionary[current]

    if final_code in evicted_codes:
        entry, prefix = evicted_codes[final_code]
        suffix = entry[len(prefix):]

        offset = None
        if prefix in string_to_idx:
            prefix_global_idx = string_to_idx[prefix]
            if prefix_global_idx >= history_start_idx:
                current_end_idx = history_start_idx + len(output_history) - 1
                offset = current_end_idx - prefix_global_idx + 1

        if offset is not None:
            if offset > 255:
                raise ValueError(f"Bug in circular buffer: offset {offset} exceeds 255! "
                                f"history_size={len(output_history)}, prefix_idx={prefix_global_idx}, "
                                f"history_start={history_start_idx}")
            # Send compact EVICT_SIGNAL
            writer.write(EVICT_SIGNAL, code_bits)
            writer.write(final_code, code_bits)
            writer.write(offset, 8)
            writer.write(ord(suffix), 8)
        else:
            # Fallback: send full entry
            writer.write(EVICT_SIGNAL, code_bits)
            writer.write(final_code, code_bits)
            writer.write(0, 8)
            writer.write(len(entry), 16)
            for c in entry:
                writer.write(ord(c), 8)

        del evicted_codes[final_code]

    writer.write(final_code, code_bits)

    current_global_idx = history_start_idx + len(output_history)
    output_history.append(current)
    string_to_idx[current] = current_global_idx

    if lru_tracker.contains(current):
        lru_tracker.use(current)

    if next_code >= threshold and code_bits < max_bits:
        code_bits += 1

    writer.write(EOF_CODE, code_bits)
    writer.close()

    print(f"Compressed: {input_file} -> {output_file}")

# ============================================================================
# LZW DECOMPRESSION WITH OPTIMIZATION (Output History)
# ============================================================================

def decompress(input_file, output_file):
    reader = BitReader(input_file)

    min_bits = reader.read(8)
    max_bits = reader.read(8)
    alphabet_size = reader.read(16)
    alphabet = [chr(reader.read(8)) for _ in range(alphabet_size)]

    dictionary = {i: char for i, char in enumerate(alphabet)}

    # Reserve codes (must match encoder)
    EOF_CODE = alphabet_size
    max_size = 1 << max_bits
    EVICT_SIGNAL = max_size - 1
    next_code = alphabet_size + 1

    code_bits = min_bits
    threshold = 1 << code_bits

    # Decoder does NOT need LRU tracker.
    # Encoder sends EVICT_SIGNAL telling decoder exactly which code to evict and the new value.

    # Output history for offset-based reconstruction (no HashMap needed on decoder side,
    # since decoder uses direct negative indexing: output_history[-offset])
    OUTPUT_HISTORY_SIZE = 255
    output_history = []

    # When EVICT_SIGNAL is received, the encoder already added an entry via eviction,
    # so the decoder must skip the normal dictionary addition on the next iteration
    skip_next_addition = False

    codeword = reader.read(code_bits)

    if codeword is None:
        raise ValueError("Corrupted file: unexpected end of file (no EOF marker)")

    if codeword == EOF_CODE:
        reader.close()
        open(output_file, 'wb').close()
        return

    prev = dictionary[codeword]

    with open(output_file, 'wb') as out:
        out.write(prev.encode('latin-1'))
        output_history.append(prev)

        while True:
            if next_code >= threshold and code_bits < max_bits:
                code_bits += 1
                threshold <<= 1

            codeword = reader.read(code_bits)

            if codeword is None:
                raise ValueError("Corrupted file: unexpected end of file (no EOF marker)")

            if codeword == EOF_CODE:
                break

            # Handle EVICT_SIGNAL: encoder evicted a code and immediately reused it
            if codeword == EVICT_SIGNAL:
                evicted_code = reader.read(code_bits)
                offset = reader.read(8)

                if offset > 0:
                    # Compact format: reconstruct from output_history[-offset] + suffix
                    suffix_byte = reader.read(8)
                    suffix = chr(suffix_byte)

                    if offset > len(output_history):
                        raise ValueError(f"Invalid offset {offset}, history size {len(output_history)}")

                    prefix = output_history[-offset]
                    new_entry = prefix + suffix
                else:
                    # Fallback format (offset=0): read full entry from stream
                    entry_length = reader.read(16)
                    new_entry = ''.join(chr(reader.read(8)) for _ in range(entry_length))

                # Overwrite the evicted code's slot with the new entry
                dictionary[evicted_code] = new_entry
                skip_next_addition = True
                continue

            if codeword in dictionary:
                current = dictionary[codeword]
            elif codeword == next_code:
                current = prev + prev[0]
            else:
                raise ValueError(f"Invalid codeword: {codeword}")

            out.write(current.encode('latin-1'))

            # Maintain output history (circular buffer)
            output_history.append(current)
            if len(output_history) > OUTPUT_HISTORY_SIZE:
                output_history.pop(0)

            # Normal dictionary addition (skipped after EVICT_SIGNAL since encoder
            # already added the entry via eviction)
            if not skip_next_addition:
                if next_code < EVICT_SIGNAL:
                    new_entry = prev + current[0]
                    dictionary[next_code] = new_entry
                    next_code += 1

            skip_next_addition = False
            prev = current

    reader.close()

    print(f"Decompressed: {input_file} -> {output_file}")

# ============================================================================
# COMMAND-LINE INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='LZW compression (optimization 2.1: minimal EVICT_SIGNAL)')
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