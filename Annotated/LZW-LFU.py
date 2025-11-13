#!/usr/bin/env python3
"""
LZW Compression Tool (LFU Mode)

Implements LZW compression with the "LFU" (Least Frequently Used) policy:
when the dictionary reaches maximum size, it evicts the least frequently
used entry to make room for new entries. Uses LRU tie-breaking for entries
with the same frequency.

Data Structure:
- Frequency buckets (doubly-linked lists) + two HashMaps for O(1) LFU operations
- key_to_node: Maps key → node for O(1) lookup
- freq_to_list: Maps frequency → list of items with that frequency
- min_freq: Tracks minimum frequency bucket for O(1) LFU finding
- Within each frequency bucket, uses LRU ordering (tail.prev = LRU)

Usage:
    Compress:   python3 LZW-LFU.py compress input.txt output.lzw --alphabet ascii
    Decompress: python3 LZW-LFU.py decompress input.lzw output.txt
"""

import sys
import argparse
from typing import TypeVar, Generic, Optional, Dict

# Predefined alphabets - add more here as needed
ALPHABETS = {
    'ascii': [chr(i) for i in range(128)],         # Standard ASCII (0-127)
    'extendedascii': [chr(i) for i in range(256)], # Extended ASCII (0-255)
    'ab': ['a', 'b']                               # Binary alphabet for testing
}

# ============================================================================
# BIT-LEVEL I/O CLASSES
# ============================================================================
# LZW uses variable-width codes (9 bits, 10 bits, etc.) but files are stored
# as bytes (8 bits). These classes handle the bit-to-byte conversion.

class BitWriter:
    """
    Writes variable-width integers as a stream of bits to a binary file.

    How it works:
    1. Accumulates bits in an integer buffer
    2. When buffer has ≥8 bits, extract and write one byte to file
    3. Clear written bits to prevent memory leak

    Buffer structure: [HIGH bits: ready to write] [LOW bits: remaining, waiting for more]
                       ^^^^^^^^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                       Extracted when ≥8 bits      Counted by n_bits
    """

    def __init__(self, filename):
        self.file = open(filename, 'wb')
        self.buffer = 0   # Integer accumulating bits
        self.n_bits = 0   # Count of remaining bits in buffer (LOW bits, not yet written)

    def write(self, value, num_bits):
        """
        Write 'num_bits' bits from 'value' to output.

        Example: write(257, 9) writes 9-bit code 0b100000001

        Process:
        1. Shift old bits left, add new bits on right: buffer = (buffer << num_bits) | value
        2. When buffer has ≥8 bits total, extract 8 from the left (high bits)
        3. Clear written bits immediately, keep remaining bits on the right (low bits)
        """
        # Add new bits to the RIGHT (low bits), old bits shift LEFT (high bits)
        self.buffer = (self.buffer << num_bits) | value
        self.n_bits += num_bits

        # Extract complete bytes (8 bits at a time) from the LEFT (high bits)
        while self.n_bits >= 8:
            self.n_bits -= 8
            # Shift right by n_bits to position the high 8 bits in the low position
            # Example: buffer=0b100000001 (9 bits), n_bits=1
            #          buffer >> 1 = 0b10000000 (the HIGH 8 bits)
            # After clearing inside loop, buffer always has ≤ n_bits, so this gives exactly 8 bits
            byte = self.buffer >> self.n_bits
            self.file.write(bytes([byte]))

            # Clear written bits immediately to prevent memory leak
            # After this, buffer has only n_bits (the remaining bits)
            # This ensures next extraction gives exactly 8 bits (no mask needed!)
            self.buffer &= (1 << self.n_bits) - 1

    def close(self):
        """Flush any remaining bits (padded with zeros) and close file."""
        if self.n_bits > 0:
            # Remaining bits are in LOW positions, shift LEFT to fill a byte
            # Example: buffer=0b101 (3 bits) → shift left 5 → 0b10100000
            # This pads the RIGHT side with zeros
            # Since buffer is cleared after each write, it only has n_bits,
            # so shifting gives a value in range [0, 255] (no mask needed)
            byte = self.buffer << (8 - self.n_bits)
            self.file.write(bytes([byte]))
        self.file.close()

class BitReader:
    """
    Reads variable-width integers from a stream of bits in a binary file.

    Mirrors BitWriter - accumulates bytes into buffer, extracts requested bits.

    Buffer structure: [HIGH bits: ready to extract] [LOW bits: remaining from last byte]
                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                       Extracted when enough bits     Counted by n_bits
    """

    def __init__(self, filename):
        self.file = open(filename, 'rb')
        self.buffer = 0   # Integer accumulating bits read from file
        self.n_bits = 0   # Count of remaining bits in buffer (LOW bits, not yet extracted)

    def read(self, num_bits):
        """
        Read 'num_bits' bits from input. Returns None at EOF.

        Example: read(9) reads a 9-bit code

        Process:
        1. Read bytes from file, add to RIGHT (low bits), old bits shift LEFT (high bits)
        2. When buffer has ≥num_bits, extract num_bits from the LEFT (high bits)
        3. Keep remaining bits on the right (low bits) for next read
        """
        # Fill buffer until we have enough bits
        while self.n_bits < num_bits:
            byte_data = self.file.read(1)
            if not byte_data:
                return None  # End of file
            # Add byte to the RIGHT (low bits), old bits shift LEFT (high bits)
            self.buffer = (self.buffer << 8) | byte_data[0]
            self.n_bits += 8

        # Extract the requested bits from the LEFT (high bits)
        self.n_bits -= num_bits
        # Shift right by n_bits to position the high bits in the low position
        # Since buffer is cleared after each read, it only has (original n_bits) data
        # After shifting right by (new n_bits), we get exactly num_bits (no mask needed)
        # Example: buffer=0b1111_1111_1111 (12 bits), want 9 bits, n_bits becomes 3
        #          buffer >> 3 = 0b1_1111_1111 (exactly 9 bits)
        value = self.buffer >> self.n_bits

        # Clear consumed bits to prevent memory leak
        # Keep only the rightmost n_bits (the remaining bits not yet used)
        self.buffer &= (1 << self.n_bits) - 1

        return value

    def close(self):
        """Close the input file."""
        self.file.close()

# ============================================================================
# LFU TRACKER DATA STRUCTURE
# ============================================================================

K = TypeVar('K')  # Key type (can be str, int, or any hashable type)

class LFUTracker(Generic[K]):
    """
    O(1) LFU tracker using frequency buckets + doubly-linked lists.
    Works with any hashable key type (strings, integers, etc).
    Uses LRU tie-breaking for entries with the same frequency.

    Type-safe generic class: LFUTracker[str] for strings, LFUTracker[int] for ints.
    """
    __slots__ = ('key_to_node', 'freq_to_list', 'min_freq')  # Memory optimization

    class Node:
        __slots__ = ('key', 'freq', 'prev', 'next')  # Memory optimization: ~40% less memory per node

        def __init__(self, key: Optional[K], freq: int) -> None:
            self.key: Optional[K] = key
            self.freq: int = freq
            self.prev: Optional['LFUTracker.Node'] = None
            self.next: Optional['LFUTracker.Node'] = None

    class FreqList:
        __slots__ = ('outer_class', 'head', 'tail')  # Memory optimization

        def __init__(self, outer_class) -> None:
            self.outer_class = outer_class
            self.head = outer_class.Node(None, 0)
            self.tail = outer_class.Node(None, 0)
            self.head.next = self.tail
            self.tail.prev = self.head

        def add_to_front(self, node: 'LFUTracker.Node') -> None:
            """Add node after head (most recently used position in this frequency bucket)."""
            node.next = self.head.next
            node.prev = self.head
            self.head.next.prev = node  # type: ignore
            self.head.next = node

        def remove(self, node: 'LFUTracker.Node') -> None:
            """Remove node from list (maintains links)."""
            node.prev.next = node.next  # type: ignore
            node.next.prev = node.prev  # type: ignore

        def is_empty(self) -> bool:
            """Check if frequency bucket is empty."""
            return self.head.next == self.tail

        def get_last(self) -> Optional['LFUTracker.Node']:
            """Get least recently used node in this frequency bucket (for LRU tie-breaking)."""
            if self.tail.prev == self.head:
                return None
            return self.tail.prev

    def __init__(self) -> None:
        self.key_to_node: Dict[K, LFUTracker.Node] = {}
        self.freq_to_list: Dict[int, LFUTracker.FreqList] = {}
        self.min_freq: int = 0

    def use(self, key: K) -> None:
        """Mark key as used. Adds key if not present, increments frequency if present."""
        node = self.key_to_node.get(key)
        if node is None:
            # New key - add to frequency 1 bucket
            node = self.Node(key, 1)
            self.key_to_node[key] = node
            if 1 not in self.freq_to_list:
                self.freq_to_list[1] = self.FreqList(self.__class__)
            self.freq_to_list[1].add_to_front(node)
            self.min_freq = 1
        else:
            # Existing key - increment frequency and move to next bucket
            old_freq = node.freq
            old_list = self.freq_to_list[old_freq]
            old_list.remove(node)

            # If we just emptied the min_freq bucket, increment min_freq
            if old_freq == self.min_freq and old_list.is_empty():
                self.min_freq = old_freq + 1

            node.freq += 1
            if node.freq not in self.freq_to_list:
                self.freq_to_list[node.freq] = self.FreqList(self.__class__)
            self.freq_to_list[node.freq].add_to_front(node)

    def find_lfu(self) -> Optional[K]:
        """Return least frequently used key (LRU tie-breaking), or None if empty."""
        min_list = self.freq_to_list.get(self.min_freq)
        if min_list is None or min_list.is_empty():
            return None
        lfu_node = min_list.get_last()
        return lfu_node.key  # type: ignore

    def remove(self, key: K) -> None:
        """Remove key from tracking."""
        node = self.key_to_node.pop(key, None)
        if node is not None:
            freq_list = self.freq_to_list[node.freq]
            freq_list.remove(node)

    def contains(self, key: K) -> bool:
        """Check if key is being tracked."""
        return key in self.key_to_node

# ============================================================================
# LZW COMPRESSION WITH LFU EVICTION
# ============================================================================

def compress(input_file, output_file, alphabet_name, min_bits=9, max_bits=16):
    """
    Compress a file using LZW with LFU eviction policy (LRU tie-breaking).

    Algorithm:
    1. Initialize dictionary with single-character entries from alphabet
    2. Read input character by character (streaming - handles huge files)
    3. Find longest match in dictionary
    4. Output code for match, add (match + next_char) to dictionary
    5. When dictionary fills (2^max_bits entries), evict LFU entry before adding new one

    LFU Eviction Details:
    - Track dictionary entries (not alphabet) with LFUTracker
    - When next_code reaches max_size-1, evict LFU entry before adding new entry
    - Increment frequency whenever an entry is used during compression
    - Among entries with same frequency, evict least recently used (LRU tie-breaking)
    - Single-char entries (alphabet) are never tracked or evicted

    Args:
        input_file: File to compress
        output_file: Compressed output file
        alphabet_name: Which alphabet to use (ascii/extendedascii/ab)
        min_bits: Starting bit width for codes (default 9)
        max_bits: Maximum bit width (default 16, max 65536 dictionary entries)

    Edge cases handled:
    - Empty file: Just write EOF marker
    - Characters not in alphabet: Raise error immediately
    - Dictionary full: Evict LFU entry (with LRU tie-breaking), then add new entry
    - Bit width increments: Check before EOF to match decoder expectations
    """
    alphabet = ALPHABETS[alphabet_name]
    valid_chars = set(alphabet)  # For O(1) validation

    # Write file header containing compression parameters
    writer = BitWriter(output_file)
    writer.write(min_bits, 8)        # 8 bits: min code width
    writer.write(max_bits, 8)        # 8 bits: max code width
    writer.write(len(alphabet), 16)  # 16 bits: alphabet size (0-65535)
    for char in alphabet:
        writer.write(ord(char), 8)   # 8 bits per character code

    # Initialize LZW dictionary with single characters
    # Example: {'a': 0, 'b': 1} for alphabet ['a', 'b']
    dictionary = {char: i for i, char in enumerate(alphabet)}

    # Reserve codes:
    # - len(alphabet): EOF marker
    # - len(alphabet)+1 to max_size-2: dictionary entries
    # - max_size-1: EVICT_SIGNAL (special synchronization code)
    EOF_CODE = len(alphabet)
    max_size = 1 << max_bits            # Maximum dictionary size (2^max_bits)
    EVICT_SIGNAL = max_size - 1         # Special signal for eviction
    next_code = len(alphabet) + 1       # Next available code

    # Variable-width encoding parameters
    code_bits = min_bits                # Current bit width (starts at min_bits)
    threshold = 1 << code_bits          # When to increment bit width (2^code_bits)

    # LFU tracker for dictionary entries (NOT alphabet entries)
    # Tracks only multi-character sequences added during compression
    lfu_tracker = LFUTracker()

    # Track evicted codes and their new values (for EVICT_SIGNAL optimization)
    # Key: code that was evicted, Value: (full_entry, prefix_at_eviction_time)
    evicted_codes = {}

    # Output history with O(1) HashMap lookup (for compact offset+suffix format)
    # Circular buffer of last 255 outputs
    OUTPUT_HISTORY_SIZE = 255
    output_history = []           # Circular buffer of recent outputs
    history_start_idx = 0         # Absolute position of first element in buffer
    string_to_idx = {}            # Maps string -> absolute position (O(1) lookup)

    # Read and compress file byte by byte (streaming for memory efficiency)
    # Binary mode to handle all file types correctly (text and binary)
    with open(input_file, 'rb') as f:
        # Read first byte
        first_byte = f.read(1)

        # Empty file
        if not first_byte:
            writer.write(EOF_CODE, min_bits)  # Just write EOF
            writer.close()
            return

        # Convert byte to character for dictionary matching
        first_char = chr(first_byte[0])

        # Validate first character is in alphabet
        if first_char not in valid_chars:
            raise ValueError(f"Byte value {first_byte[0]} at position 0 not in alphabet")

        current = first_char  # Current phrase being matched
        pos = 1  # Track position for better error messages

        # Main LZW compression loop
        while True:
            byte_data = f.read(1)  # Read next byte
            if not byte_data:          # End of input
                break

            # Convert byte to character
            char = chr(byte_data[0])

            # Validate character
            if char not in valid_chars:
                raise ValueError(f"Byte value {byte_data[0]} at position {pos} not in alphabet")
            pos += 1

            combined = current + char  # Try extending current phrase

            if combined in dictionary:
                # Phrase exists in dictionary - keep extending
                current = combined
            else:
                # Phrase not in dictionary - output code and add new entry

                # About to output code for current phrase
                output_code = dictionary[current]

                # Check if this code was evicted and is being reused
                # This is the "evict-then-use" pattern that requires EVICT_SIGNAL
                if output_code in evicted_codes:
                    # Encoder is about to use a code that was evicted!
                    # Decoder won't know the new value - SEND SIGNAL

                    # Unpack stored entry and prefix
                    entry, prefix = evicted_codes[output_code]

                    # Compute suffix (character that extends prefix to entry)
                    suffix = entry[len(prefix):]
                    if len(suffix) != 1:
                        raise ValueError(f"Logic error: suffix should be 1 char, got {len(suffix)}")

                    # Try O(1) HashMap lookup for prefix position in output history
                    # If prefix is in recent history, we can send compact offset+suffix format
                    offset = None
                    if prefix in string_to_idx:
                        prefix_global_idx = string_to_idx[prefix]
                        # Check if still in valid buffer range (circular buffer may have evicted it)
                        if prefix_global_idx >= history_start_idx:
                            # Calculate offset from end of current history
                            current_end_idx = history_start_idx + len(output_history) - 1
                            offset = current_end_idx - prefix_global_idx + 1

                    if offset is not None:
                        if offset > 255:
                            raise ValueError(f"Bug in circular buffer: offset {offset} exceeds 255!")
                        # Prefix found in recent history! Send compact EVICT_SIGNAL
                        writer.write(EVICT_SIGNAL, code_bits)
                        writer.write(output_code, code_bits)
                        writer.write(offset, 8)       # 1 byte offset (1-255)
                        writer.write(ord(suffix), 8)  # 1 byte suffix
                    else:
                        # Prefix not in recent history - fall back to full entry format
                        writer.write(EVICT_SIGNAL, code_bits)
                        writer.write(output_code, code_bits)
                        writer.write(0, 8)            # offset=0 signals "full entry follows"
                        writer.write(len(entry), 16)  # 16 bits for string length
                        for c in entry:
                            writer.write(ord(c), 8)   # 8 bits per character

                    # Remove from evicted_codes since we've now synced it
                    del evicted_codes[output_code]

                # Output code for current phrase (repeated)
                writer.write(output_code, code_bits)

                # Add current output to history with O(1) HashMap tracking
                current_global_idx = history_start_idx + len(output_history)
                output_history.append(current)
                string_to_idx[current] = current_global_idx

                # Maintain circular buffer size (remove oldest when exceeds 255)
                if len(output_history) > OUTPUT_HISTORY_SIZE:
                    output_history.pop(0)
                    history_start_idx += 1

                # Update LFU if current phrase is tracked (not single char from alphabet)
                if lfu_tracker.contains(current):
                    lfu_tracker.use(current)

                # Add new entry to dictionary
                if next_code < EVICT_SIGNAL:
                    # Dictionary not full yet - add normally

                    # Check if we need to increase bit width
                    if next_code >= threshold and code_bits < max_bits:
                        code_bits += 1
                        threshold <<= 1

                    # Add new phrase to dictionary
                    dictionary[combined] = next_code
                    lfu_tracker.use(combined)  # Mark as most recently used
                    next_code += 1
                else:
                    # Dictionary FULL - evict LFU entry and reuse its code
                    lfu_entry = lfu_tracker.find_lfu()
                    if lfu_entry is not None:
                        # Get the code of the LFU entry
                        lfu_code = dictionary[lfu_entry]

                        # Remove old entry from dictionary and LFU tracker
                        del dictionary[lfu_entry]
                        lfu_tracker.remove(lfu_entry)

                        # Add new entry at evicted code position
                        dictionary[combined] = lfu_code
                        lfu_tracker.use(combined)

                        # Track eviction with both full entry and prefix
                        evicted_codes[lfu_code] = (combined, current)
                        # Note: next_code stays at EVICT_SIGNAL (doesn't increment)

                # Start new phrase with current character
                current = char

    # Write final phrase
    final_code = dictionary[current]

    # Check if final code was evicted
    if final_code in evicted_codes:
        entry, prefix = evicted_codes[final_code]
        suffix = entry[len(prefix):]

        # Try O(1) HashMap lookup for prefix position
        offset = None
        if prefix in string_to_idx:
            prefix_global_idx = string_to_idx[prefix]
            if prefix_global_idx >= history_start_idx:
                current_end_idx = history_start_idx + len(output_history) - 1
                offset = current_end_idx - prefix_global_idx + 1

        if offset is not None:
            if offset > 255:
                raise ValueError(f"Bug in circular buffer: offset {offset} exceeds 255!")
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

    # Add final output to history
    current_global_idx = history_start_idx + len(output_history)
    output_history.append(current)
    string_to_idx[current] = current_global_idx

    # Update LFU for final phrase if it's tracked
    if lfu_tracker.contains(current):
        lfu_tracker.use(current)

    # Check if decoder will increment bit width before reading EOF
    # The decoder increments AFTER reading each codeword but BEFORE reading the next
    # So after reading the final phrase, if next_code >= threshold, decoder increments
    # Therefore we must write EOF with the SAME (potentially incremented) bit width
    # This allows EOF to work with any min_bits/alphabet combination
    if next_code >= threshold and code_bits < max_bits:
        code_bits += 1

    # Write EOF marker (uses alphabet_size as the EOF code)
    writer.write(EOF_CODE, code_bits)
    writer.close()
    print(f"Compressed: {input_file} -> {output_file}")

# ============================================================================
# LZW DECOMPRESSION WITH LFU EVICTION
# ============================================================================

def decompress(input_file, output_file):
    """
    Decompress a file compressed with LZW LFU mode (LRU tie-breaking).

    Algorithm:
    1. Read header to get compression parameters and alphabet
    2. Initialize dictionary with single-character entries
    3. Read codes from compressed file
    4. Decode each code using dictionary
    5. Add new entries to dictionary as we decode (mirroring encoder)
    6. When dictionary fills, evict LFU entry before adding new one
    7. Write decompressed output incrementally (streaming for memory efficiency)

    LFU Eviction Details:
    - Track dictionary codes (not alphabet codes) with LFUTracker
    - When next_code reaches max_size-1, evict LFU before adding new entry
    - Increment frequency whenever a code is read from compressed file
    - Among codes with same frequency, evict least recently used (LRU tie-breaking)
    - Single-char codes (alphabet) are never tracked or evicted
    - Invalidated entries (set to None) are handled by codeword == next_code case

    Edge cases handled:
    - Empty file: Just EOF marker, create empty output
    - Special LZW case: Code not yet in dictionary (codeword == next_code)
      This happens when pattern like "aba" is encoded as "ab" + "a"
    - Bit width increments: Match encoder's increments exactly
    - Dictionary full: Evict LFU entry (with LRU tie-breaking), then add new entry
    - Evicted entry re-referenced: Handled by special case logic
    """
    reader = BitReader(input_file)

    # Read header
    min_bits = reader.read(8)
    max_bits = reader.read(8)
    alphabet_size = reader.read(16)
    alphabet = [chr(reader.read(8)) for _ in range(alphabet_size)]

    # Initialize dictionary with single characters
    # Example: {0: 'a', 1: 'b'} for alphabet ['a', 'b']
    dictionary = {i: char for i, char in enumerate(alphabet)}

    # Reserve codes (must match encoder):
    # - alphabet_size: EOF marker
    # - alphabet_size+1 to max_size-2: dictionary entries
    # - max_size-1: EVICT_SIGNAL
    EOF_CODE = alphabet_size
    max_size = 1 << max_bits
    EVICT_SIGNAL = max_size - 1
    next_code = alphabet_size + 1  # Next available dictionary code

    # Variable-width decoding parameters (must match encoder)
    code_bits = min_bits
    threshold = 1 << code_bits

    # NOTE: Decoder does NOT need LFU tracker!
    # Encoder sends EVICT_SIGNAL telling decoder exactly which code to evict and the new value

    # Output history for offset-based reconstruction
    OUTPUT_HISTORY_SIZE = 255
    output_history = []

    # Flag to skip dictionary addition after EVICT_SIGNAL
    skip_next_addition = False

    # Read first codeword
    codeword = reader.read(code_bits)

    # Check for file corruption
    if codeword is None:
        raise ValueError("Corrupted file: unexpected end of file (no EOF marker)")

    # Empty file (just EOF)
    if codeword == EOF_CODE:
        reader.close()
        open(output_file, 'wb').close()  # Create empty file
        return

    # Decode first codeword and write to output
    # First codeword is always part of dictionary
    prev = dictionary[codeword]  # Previous decoded string

    # Write output incrementally (streaming - handles huge files)
    # Binary mode to handle all file types correctly (text and binary)
    with open(output_file, 'wb') as out:
        out.write(prev.encode('latin-1'))

        # Add first output to history
        output_history.append(prev)

        # Main decompression loop
        while True:
            # Check if we need to increase bit width
            # This happens AFTER processing previous codeword, BEFORE reading next one
            # Encoder checks this same condition before writing EOF, so bit widths match
            if next_code >= threshold and code_bits < max_bits:
                code_bits += 1
                threshold <<= 1

            # Read next codeword
            codeword = reader.read(code_bits)

            # Check for file corruption
            if codeword is None:
                raise ValueError("Corrupted file: unexpected end of file (no EOF marker)")

            # Check for EOF
            if codeword == EOF_CODE:
                break

            # Handle EVICT_SIGNAL (evict-then-use pattern detected by encoder)
            if codeword == EVICT_SIGNAL:
                # Encoder is evicting an entry and adding a new one
                # Format: [EVICT_SIGNAL] [code] [offset] [suffix] or [EVICT_SIGNAL] [code] [0] [full_entry]

                # Read which code is being evicted
                evicted_code = reader.read(code_bits)

                # Read offset (1 byte)
                offset = reader.read(8)

                if offset > 0:
                    # Compact format - reconstruct from offset+suffix

                    # Read suffix (1 byte)
                    suffix_byte = reader.read(8)
                    suffix = chr(suffix_byte)

                    # Look back in output history to find prefix
                    if offset > len(output_history):
                        raise ValueError(f"Invalid offset {offset}, history size {len(output_history)}")

                    # Get prefix from output history using negative indexing
                    prefix = output_history[-offset]

                    # Reconstruct full entry from prefix + suffix
                    new_entry = prefix + suffix
                else:
                    # offset=0 signals fallback to full entry format
                    # Read entry length and full entry
                    entry_length = reader.read(16)
                    new_entry = ''.join(chr(reader.read(8)) for _ in range(entry_length))

                # Add new entry at the evicted code position (EVICT_SIGNAL tells us exactly what to do)
                dictionary[evicted_code] = new_entry

                # Skip dictionary addition on next iteration
                skip_next_addition = True

                # Don't output anything, don't update prev, continue to next code
                continue

            # Decode codeword
            if codeword in dictionary:
                # Normal case: code exists in dictionary
                current = dictionary[codeword]
            elif codeword == next_code:
                # SPECIAL LZW EDGE CASE:
                # Encoder output code for entry it's about to add!
                # This happens when pattern repeats immediately: "aba" -> "ab" + "a"
                # Solution: current = prev + first char of prev
                current = prev + prev[0]
            else:
                # Invalid codeword - corrupted file
                raise ValueError(f"Invalid codeword: {codeword}")

            # Write decoded string as bytes
            out.write(current.encode('latin-1'))

            # Add to output history (circular buffer)
            output_history.append(current)
            if len(output_history) > OUTPUT_HISTORY_SIZE:
                output_history.pop(0)

            # Add new entry to dictionary (NO eviction logic needed - EVICT_SIGNAL handles everything!)
            # Skip if previous iteration received EVICT_SIGNAL
            if not skip_next_addition:
                if next_code < EVICT_SIGNAL:
                    # Dictionary not full yet - add normally
                    new_entry = prev + current[0]
                    dictionary[next_code] = new_entry
                    next_code += 1
                # No else block - when dictionary is full, EVICT_SIGNAL tells us what to do!

            # Reset skip flag
            skip_next_addition = False

            # Update previous string for next iteration
            prev = current

    reader.close()
    print(f"Decompressed: {input_file} -> {output_file}")

# ============================================================================
# COMMAND-LINE INTERFACE
# ============================================================================

def main():
    """Parse command-line arguments and run compression or decompression."""
    parser = argparse.ArgumentParser(description='LZW compression (LFU mode)')
    sub = parser.add_subparsers(dest='mode', required=True)

    # Compress subcommand
    c = sub.add_parser('compress')
    c.add_argument('input')
    c.add_argument('output')
    c.add_argument('--alphabet', required=True, choices=list(ALPHABETS.keys()))
    c.add_argument('--min-bits', type=int, default=9)
    c.add_argument('--max-bits', type=int, default=16)

    # Decompress subcommand
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
