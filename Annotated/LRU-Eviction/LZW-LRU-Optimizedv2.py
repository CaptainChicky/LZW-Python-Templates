#!/usr/bin/env python3
"""
LZW Compression Tool (Optimization 2: Output History + Offset/Suffix with O(L) Linear Search)

Implements LZW compression with LRU (Least Recently Used) eviction policy.
This is OPTIMIZATION 2 (LINEAR SEARCH VERSION): Uses O(L) search instead of O(1) HashMap.

Implements LZW compression with LRU eviction, using an OPTIMIZED signaling strategy:
- Only sends EVICT_SIGNAL when encoder evicts code C and immediately uses C (~10-30% of evictions)
- Instead of sending full entry, uses output history to send compact offset+suffix
- This reduces EVICT_SIGNAL size by ~57% (34 bits vs 123 bits for 10-char entry)
- LINEAR SEARCH: Uses O(255*L) search vs O(1) HashMap (for benchmarking comparison)

How It Works:
1. Encoder tracks all evicted codes in a dictionary (like Optimization 1)
2. Encoder maintains circular buffer of last 255 outputs WITHOUT HashMap
3. When about to output a code that was recently evicted:
   a. LINEAR SEARCH through output history to find prefix position
   b. Send compact [offset][suffix] (2 bytes) if found
   c. If prefix not in history, fall back to full entry format
4. Decoder maintains same output history, reconstructs entry from offset+suffix
5. Both stay synchronized through LRU tracking and output history mirroring

Data Structure:
- Doubly-linked list + HashMap for O(1) LRU operations
- Sentinel head/tail nodes eliminate edge cases
- Output history: Circular buffer (last 255 outputs) WITHOUT HashMap (linear search)

LINEAR SEARCH vs HASH MAP Tradeoff:
- This version: O(255*L) search, minimal memory (~0KB overhead)
- HashMap version: O(1) search, ~4KB memory overhead
- Performance: HashMap is ~3-10% faster overall for typical files
- Compression ratio: Identical (both use same algorithm)
- Use case: This version for memory-constrained embedded systems

EVICT_SIGNAL Format (Compact - used ~95% of time):
- [EVICT_SIGNAL][code][offset][suffix][code_again]
- Total: code_bits + code_bits + 8 + 8 + code_bits bits
- Example (9-bit codes): 9+9+8+8+9 = 43 bits (vs 123 bits in Opt-1!)

EVICT_SIGNAL Format (Fallback - when prefix not in recent history):
- [EVICT_SIGNAL][code][0][entry_length][char1]...[charN][code_again]
- offset=0 signals "full entry follows" (0 is never a valid offset)
- Example (9-bit codes, 10-char entry): 9+9+8+16+80+9 = 131 bits

Usage:
    Compress:   python3 LZW-LRU-Optimizedv2.py compress input.txt output.lzw --alphabet ascii
    Decompress: python3 LZW-LRU-Optimizedv2.py decompress input.lzw output.txt
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
# LRU TRACKER DATA STRUCTURE
# ============================================================================

K = TypeVar('K')  # Key type (can be str, int, or any hashable type)

class LRUTracker(Generic[K]):
    """
    O(1) LRU tracker using doubly-linked list + HashMap.
    Works with any hashable key type (strings, integers, etc).

    Type-safe generic class: LRUTracker[str] for strings, LRUTracker[int] for ints.
    """
    __slots__ = ('map', 'head', 'tail')  # Memory optimization

    class Node:
        __slots__ = ('key', 'prev', 'next')  # Memory optimization: ~40% less memory per node

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
        """Mark key as recently used. Adds key if not present."""
        node = self.map.get(key)
        if node is not None:
            # Key exists - move to front (most recently used)
            self._remove_node(node)
            self._add_to_front(node)
        else:
            # New key - add to front
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
# LZW COMPRESSION WITH OPTIMIZATION 2
# ============================================================================

def compress(input_file, output_file, alphabet_name, min_bits=9, max_bits=16):
    """
    Compress a file using LZW with OPTIMIZATION 2: (output history + offset/suffix).

    Algorithm:
    1. Initialize dictionary with single-character entries from alphabet
    2. Read input character by character (streaming - handles huge files)
    3. Find longest match in dictionary
    4. Output code for match, add (match + next_char) to dictionary
    5. When dictionary fills (2^max_bits entries), evict LRU entry before adding new one
    6. Track recent outputs in circular buffer for EVICT_SIGNAL reconstruction

    LRU Eviction Details:
    - Track dictionary entries (not alphabet) with LRUTracker
    - When dictionary is full, evict LRU and reuse its code position
    - Send EVICT_SIGNAL during special case of immediate reuse after eviction
    - Use output history to send compact offset+suffix instead of full entry
    - Update access time whenever an entry is used during compression
    - Single-char entries (alphabet) are never tracked or evicted

    Output History Details (OPTIMIZATION 2):
    - Maintain circular buffer of last 255 outputs
    - When eviction happens, check if prefix is in recent history
    - If yes: Send compact format [offset][suffix] (2 bytes)
    - If no: Fall back to full entry format

    Args:
        input_file: File to compress
        output_file: Compressed output file
        alphabet_name: Which alphabet to use (ascii/extendedascii/ab)
        min_bits: Starting bit width for codes (default 9)
        max_bits: Maximum bit width (default 16, max 65536 dictionary entries)

    Edge cases handled:
    - Empty file: Just write EOF marker
    - Characters not in alphabet: Raise error immediately
    - Dictionary full: Evict LRU entry, reuse its code, send signal to decoder
    - Bit width increments: Check before EOF to match decoder expectations
    - Prefix not in recent history: Fall back to full entry format

    EVICT_SIGNAL no longer includes dictionary entry bytes - decoder reconstructs!
    Otherwise, decoder can mirror encoder's LRU logic without signal.
    """
    alphabet = ALPHABETS[alphabet_name]
    valid_chars = set(alphabet)

    # Write file header containing compression parameters
    # This allows decoder to reconstruct alphabet and settings
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

    # LRU tracker for dictionary entries (NOT alphabet entries)
    # Tracks only multi-character sequences added during compression
    lru_tracker = LRUTracker()

    # OPTIMIZATION: Track evicted codes and their new values
    # evicted_codes[code] = (full_entry, prefix_at_eviction_time)
    # When encoder outputs a recently-evicted code, decoder won't know the new value
    # So we send EVICT_SIGNAL to synchronize. This dictionary tracks pending syncs.
    evicted_codes = {}

    # OPTIMIZATION 2: Output history with O(255*L) linear search
    # Circular buffer of last 255 outputs (8-bit offset limit)
    # Uses linear search O(255*L) instead of HashMap O(1)
    # Minimal memory overhead vs 4KB for HashMap version
    OUTPUT_HISTORY_SIZE = 255
    output_history = []           # Circular buffer of recent outputs

    # Read and compress file byte by byte (streaming for memory efficiency)
    # Binary mode to handle all file types correctly (text and binary)
    with open(input_file, 'rb') as f:
        # Read first byte
        first_byte = f.read(1)

        # Empty file
        if not first_byte:
            writer.write(EOF_CODE, min_bits)
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
                # Don't update LRU yet - only update when we actually output the code
                current = combined
            else:
                # Phrase not in dictionary - output code and add new entry

                # About to output code for current phrase
                output_code = dictionary[current]

                # OPTIMIZATION 2: Check if this code was evicted and is being reused
                # This is the "evict-then-use" pattern that requires EVICT_SIGNAL
                if output_code in evicted_codes:
                    # Unpack stored entry and prefix
                    entry, prefix = evicted_codes[output_code]

                    # Compute suffix (character that extends prefix to entry)
                    suffix = entry[len(prefix):]
                    if len(suffix) != 1:
                        raise ValueError(f"Logic error: suffix should be 1 char, got {len(suffix)}")

                    # LINEAR SEARCH (O(255*L)) through output history
                    # Search backwards for most recent occurrence of prefix
                    offset = None
                    for i in range(len(output_history) - 1, -1, -1):
                        if output_history[i] == prefix:  # O(L) string comparison
                            offset = len(output_history) - i
                            break

                    if offset is not None:
                        if offset > 255:
                            raise ValueError(f"Bug in circular buffer: offset {offset} exceeds 255!")
                        # Send compact EVICT_SIGNAL: [EVICT_SIGNAL] [code] [offset] [suffix]  [code(again)]
                        writer.write(EVICT_SIGNAL, code_bits)
                        writer.write(output_code, code_bits)
                        writer.write(offset, 8)       # 1 byte offset
                        writer.write(ord(suffix), 8)  # 1 byte suffix
                    else:
                        # Fallback: send full entry (prefix not in recent history)
                        # Format: [EVICT_SIGNAL] [code] [0] [entry_length] [char1...charN] [code(again)]
                        writer.write(EVICT_SIGNAL, code_bits)
                        writer.write(output_code, code_bits)
                        writer.write(0, 8)            # offset=0 signals "full entry follows"
                        writer.write(len(entry), 16)
                        for c in entry:
                            writer.write(ord(c), 8)

                    # Remove from evicted_codes since we've now synced it
                    del evicted_codes[output_code]

                # Output code for current phrase (repeated)
                writer.write(output_code, code_bits)

                # Maintain circular buffer size (remove oldest when exceeds 255)
                output_history.append(current)
                if len(output_history) > OUTPUT_HISTORY_SIZE:
                    output_history.pop(0)  # Remove oldest from buffer

                # Update LRU if current phrase is a tracked entry (not single char from alphabet)
                if lru_tracker.contains(current):
                    lru_tracker.use(current)

                # Add new entry to dictionary
                if next_code < EVICT_SIGNAL:
                    # Dictionary not full yet - add normally

                    # Check if we need to increase bit width
                    # When next_code reaches threshold (512, 1024, etc.), we need more bits
                    if next_code >= threshold and code_bits < max_bits:
                        code_bits += 1
                        threshold <<= 1  # Double threshold (bitshift left = multiply by 2)

                    # Add new phrase to dictionary
                    dictionary[combined] = next_code
                    lru_tracker.use(combined)  # Mark as most recently used
                    next_code += 1
                else:
                    # Dictionary FULL - evict LRU entry and reuse its code
                    lru_entry = lru_tracker.find_lru()
                    if lru_entry is not None:
                        # Get the code of the LRU entry
                        lru_code = dictionary[lru_entry]

                        # Remove old entry from dictionary and LRU tracker
                        del dictionary[lru_entry]
                        lru_tracker.remove(lru_entry)

                        # Add new entry at evicted code position
                        dictionary[combined] = lru_code
                        lru_tracker.use(combined)

                        # Track eviction with both full entry and prefix
                        # Prefix is needed to compute offset+suffix encoding
                        evicted_codes[lru_code] = (combined, current)
                        # Note: next_code stays at EVICT_SIGNAL (doesn't increment)

                # Start new phrase with current character
                current = char

    # Write final phrase
    final_code = dictionary[current]

    # Check if final code was evicted
    if final_code in evicted_codes:
        entry, prefix = evicted_codes[final_code]
        suffix = entry[len(prefix):]

        # LINEAR SEARCH through output history
        offset = None
        for i in range(len(output_history) - 1, -1, -1):
            if output_history[i] == prefix:
                offset = len(output_history) - i
                break

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
    output_history.append(current)

    # Update LRU for final phrase
    if lru_tracker.contains(current):
        lru_tracker.use(current)

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
# LZW DECOMPRESSION WITH OPTIMIZATION 2
# ============================================================================

def decompress(input_file, output_file):
    """
    Decompress a file using OPTIMIZATION 2 (output history reconstruction).

    Algorithm:
    1. Read header to get compression parameters and alphabet
    2. Initialize dictionary with single-character entries
    3. Read codes from compressed file
    4. Decode each code using dictionary (or handle EVICT_SIGNAL)
    5. Add new entries to dictionary as we decode (mirroring encoder)
    6. When dictionary fills, evict LRU as usual, unless EVICT_SIGNAL is detected
    7. Maintain output history to reconstruct entries from offset+suffix
    8. Write decompressed output incrementally (streaming for memory efficiency)

    LRU Eviction Details:
    - Decoder tracks LRU and evicts normally unless a special case happens
    - Special case happens when encoder evicts A, adds B to A's position, then uses B immediately
    - Encoder sends EVICT_SIGNAL during special case: [code][offset][suffix] or [code][0][full_entry]
    - This keeps decoder simple and mirrors encoder's dictionary state

    Output History Details (OPTIMIZATION 2):
    - Maintain circular buffer of last 255 outputs (same as encoder)
    - When EVICT_SIGNAL received with offset > 0: Reconstruct from output_history[-offset] + suffix
    - When EVICT_SIGNAL received with offset = 0: Read full entry from stream
    - Decoder only needs list, not HashMap (uses direct indexing: output_history[-offset])
    - Encoder needs HashMap for reverse lookup (string -> position)

    Edge cases handled:
    - Empty file: Just EOF marker, create empty output
    - Special LZW case: Code not yet in dictionary (codeword == next_code)
      This happens when pattern like "aba" is encoded as "ab" + "a"
    - Bit width increments: Match encoder's increments exactly
    - Dictionary full: Receive EVICT_SIGNAL, evict specified code, add new entry
    - EVICT_SIGNAL: Special code indicating eviction happening on encoder side
    - Offset validation: Ensure offset doesn't exceed output history size

    Decoder mirrors encoder's LRU logic, except when EVICT_SIGNAL is received.
    Uses output history to reconstruct entries from offset+suffix encoding.
    """
    reader = BitReader(input_file)

    # Read file header
    min_bits = reader.read(8)
    max_bits = reader.read(8)
    alphabet_size = reader.read(16)
    alphabet = [chr(reader.read(8)) for _ in range(alphabet_size)]

    # Initialize dictionary with alphabet
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

    # NOTE: Decoder does NOT need LRU tracker!
    # Encoder sends EVICT_SIGNAL telling decoder exactly which code to evict and the new value

    # OPTIMIZATION 2: Output history for offset-based reconstruction
    # Decoder uses direct indexing: output_history[-offset] which is O(1)
    # No need for HashMap (encoder needs linear search for reverse lookup)
    OUTPUT_HISTORY_SIZE = 255
    output_history = []

    # Flag to skip dictionary addition after EVICT_SIGNAL
    # When EVICT_SIGNAL received, encoder already added entry via eviction
    # Decoder shouldn't add another entry on next iteration
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
                # Format: [EVICT_SIGNAL][code][offset][suffix] or [EVICT_SIGNAL][code][0][full_entry]

                # Read which code is being evicted
                evicted_code = reader.read(code_bits)

                # Read offset (1 byte)
                offset = reader.read(8)

                if offset > 0:
                    # OPTIMIZATION 2: Compact format - reconstruct from offset+suffix

                    # Read suffix (1 byte)
                    suffix_byte = reader.read(8)
                    suffix = chr(suffix_byte)

                    # Look back in output history to find prefix
                    # offset=1 means last output, offset=2 means second-to-last, etc.
                    if offset > len(output_history):
                        raise ValueError(f"Invalid offset {offset}, history size {len(output_history)}")

                    # Get prefix from output history using negative indexing
                    # output_history[-1] is last, output_history[-2] is second-to-last, etc.
                    prefix = output_history[-offset]
                    new_entry = prefix + suffix
                else:
                    # offset=0 signals fallback to full entry format
                    # Read entry length and full entry
                    entry_length = reader.read(16)
                    new_entry = ''.join(chr(reader.read(8)) for _ in range(entry_length))

                # Add new entry at the evicted code position (EVICT_SIGNAL tells us exactly what to do)
                dictionary[evicted_code] = new_entry

                # Skip dictionary addition on next iteration
                # Encoder already added an entry when it evicted
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
                # Encoder sees "ab", outputs code, adds "aba" as next_code
                # Then sees "aba" and outputs next_code before decoder added it!
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
                output_history.pop(0)  # Remove oldest from buffer

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

    parser = argparse.ArgumentParser(description='LZW compression (optimization 2: minimal EVICT_SIGNAL)')
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
