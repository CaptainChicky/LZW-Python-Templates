#!/usr/bin/env python3
"""
LZW Compression Tool (Freeze Mode)

Implements LZW compression with the "freeze" policy: when the dictionary
reaches maximum size, it stops adding new entries and continues using
the existing dictionary.

Usage:
    Compress:   python3 LZW-Freeze.py compress input.txt output.lzw --alphabet ascii
    Decompress: python3 LZW-Freeze.py decompress input.lzw output.txt
"""

import sys
import argparse

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
# LZW COMPRESSION
# ============================================================================

def compress(input_file, output_file, alphabet_name, min_bits=9, max_bits=16):
    """
    Compress a file using LZW with freeze policy.

    Algorithm:
    1. Initialize dictionary with single-character entries from alphabet
    2. Read input character by character (streaming - handles huge files)
    3. Find longest match in dictionary
    4. Output code for match, add (match + next_char) to dictionary
    5. When dictionary fills (2^max_bits entries), stop adding (freeze)

    Args:
        input_file: File to compress
        output_file: Compressed output file
        alphabet_name: Which alphabet to use (ascii/extendedascii/ab)
        min_bits: Starting bit width for codes (default 9)
        max_bits: Maximum bit width (default 16, max 65536 dictionary entries)

    Edge cases handled:
    - Empty file: Just write EOF marker
    - Characters not in alphabet: Raise error immediately
    - Dictionary full: Stop adding, continue with existing entries (freeze)
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

    # Reserve code for EOF (End Of File marker)
    # If alphabet has 2 chars, EOF = 2, next available code = 3
    EOF_CODE = len(alphabet)
    next_code = len(alphabet) + 1

    # Variable-width encoding parameters
    code_bits = min_bits                # Current bit width (starts at min_bits)
    max_size = 1 << max_bits            # Maximum dictionary size (2^max_bits)
    threshold = 1 << code_bits          # When to increment bit width (2^code_bits)

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

                # Output code for current phrase
                writer.write(dictionary[current], code_bits)

                # Add new entry to dictionary if not full (FREEZE policy)
                if next_code < max_size:
                    # Check if we need to increase bit width
                    # When next_code reaches threshold (512, 1024, etc.), we need more bits
                    if next_code >= threshold and code_bits < max_bits:
                        code_bits += 1
                        threshold <<= 1  # Double threshold (bitshift left = multiply by 2)

                    # Add new phrase to dictionary
                    dictionary[combined] = next_code
                    next_code += 1

                # else freeze policy, do nothing

                # Start new phrase with current character
                current = char

    # Write final phrase
    writer.write(dictionary[current], code_bits)

    # Check if decoder will increment bit width before reading EOF
    # The decoder increments AFTER reading each codeword but BEFORE reading the next
    # So after reading the final phrase, if next_code >= threshold, decoder increments
    # Therefore we must write EOF with the SAME (potentially incremented) bit width
    # This allows EOF to work with any min_bits/alphabet combination
    if next_code >= threshold and code_bits < max_bits:
        code_bits += 1
        # do not need to update "threshold <<= 1" because its the last check

    # Write EOF marker (uses alphabet_size as the EOF code)
    writer.write(EOF_CODE, code_bits)
    writer.close()
    print(f"Compressed: {input_file} -> {output_file}")

# ============================================================================
# LZW DECOMPRESSION
# ============================================================================

def decompress(input_file, output_file):
    """
    Decompress a file compressed with LZW freeze mode.

    Algorithm:
    1. Read header to get compression parameters and alphabet
    2. Initialize dictionary with single-character entries
    3. Read codes from compressed file
    4. Decode each code using dictionary
    5. Add new entries to dictionary as we decode (mirroring encoder)
    6. Write decompressed output incrementally (streaming for memory efficiency)

    Edge cases handled:
    - Empty file: Just EOF marker, create empty output
    - Special LZW case: Code not yet in dictionary (codeword == next_code)
      This happens when pattern like "aba" is encoded as "ab" + "a"
    - Bit width increments: Match encoder's increments exactly
    - Dictionary full: Stop adding entries (freeze)
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

    # EOF is alphabet_size
    EOF_CODE = alphabet_size
    next_code = alphabet_size + 1  # Next available dictionary code (alphabet_size reserved for EOF)

    # Variable-width decoding parameters (must match encoder)
    code_bits = min_bits
    max_size = 1 << max_bits
    threshold = 1 << code_bits

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
        # Convert string to bytes using latin-1 encoding
        out.write(prev.encode('latin-1'))

        # Main LZW decompression loop
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

            # Add new entry to dictionary if not full (FREEZE policy)
            if next_code < max_size:
                # New entry is: previous string + first char of current string
                # This mirrors what encoder did
                dictionary[next_code] = prev + current[0]
                next_code += 1

            # else freeze policy, do nothing

            # Update previous string for next iteration
            prev = current

    reader.close()
    print(f"Decompressed: {input_file} -> {output_file}")

# ============================================================================
# COMMAND-LINE INTERFACE
# ============================================================================

def main():
    """Parse command-line arguments and run compression or decompression."""
    parser = argparse.ArgumentParser(description='LZW compression (freeze mode)')
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
