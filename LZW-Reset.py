#!/usr/bin/env python3
"""
LZW Compression Tool (Reset Mode)

Implements LZW compression with the "reset" policy: when the dictionary
reaches maximum size, it outputs a RESET code, clears the dictionary back
to the initial alphabet, and continues compression with a fresh dictionary.

Usage:
    Compress:   python3 LZW-Reset.py compress input.txt output.lzw --alphabet ascii
    Decompress: python3 LZW-Reset.py decompress input.lzw output.txt
"""

import sys
import argparse

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
# LZW COMPRESSION
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

    # Reserve codes: EOF = alphabet_size, RESET = alphabet_size + 1
    # If alphabet has 2 chars: codes 0,1 are chars, EOF=2, RESET=3, next available=4
    EOF_CODE = len(alphabet)
    RESET_CODE = len(alphabet) + 1
    next_code = len(alphabet) + 2

    code_bits = min_bits
    max_size = 1 << max_bits
    threshold = 1 << code_bits

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
                current = combined
            else:
                writer.write(dictionary[current], code_bits)

                if next_code < max_size:
                    if next_code >= threshold and code_bits < max_bits:
                        code_bits += 1
                        threshold <<= 1

                    dictionary[combined] = next_code
                    next_code += 1
                else:
                    # RESET policy: dictionary full, so emit RESET code and start fresh
                    if next_code >= threshold and code_bits < max_bits:
                        code_bits += 1
                        threshold <<= 1

                    writer.write(RESET_CODE, code_bits)

                    # Clear dictionary back to alphabet-only
                    dictionary = {char: i for i, char in enumerate(alphabet)}
                    next_code = len(alphabet) + 2  # Skip EOF and RESET codes
                    code_bits = min_bits
                    threshold = 1 << code_bits

                current = char

    writer.write(dictionary[current], code_bits)

    if next_code >= threshold and code_bits < max_bits:
        code_bits += 1

    writer.write(EOF_CODE, code_bits)
    writer.close()
    print(f"Compressed: {input_file} -> {output_file}")

# ============================================================================
# LZW DECOMPRESSION
# ============================================================================

def decompress(input_file, output_file):
    reader = BitReader(input_file)

    min_bits = reader.read(8)
    max_bits = reader.read(8)
    alphabet_size = reader.read(16)
    alphabet = [chr(reader.read(8)) for _ in range(alphabet_size)]

    dictionary = {i: char for i, char in enumerate(alphabet)}

    # Reserve codes: EOF = alphabet_size, RESET = alphabet_size + 1
    EOF_CODE = alphabet_size
    RESET_CODE = alphabet_size + 1
    next_code = alphabet_size + 2

    code_bits = min_bits
    max_size = 1 << max_bits
    threshold = 1 << code_bits

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

        while True:
            if next_code >= threshold and code_bits < max_bits:
                code_bits += 1
                threshold <<= 1

            codeword = reader.read(code_bits)

            if codeword is None:
                raise ValueError("Corrupted file: unexpected end of file (no EOF marker)")

            if codeword == EOF_CODE:
                break

            # RESET MODE: clear dictionary back to alphabet-only, reset bit width,
            # then read the next codeword at min_bits (no new entry added after reset)
            if codeword == RESET_CODE:
                dictionary = {i: alphabet[i] for i in range(alphabet_size)}
                next_code = alphabet_size + 2
                code_bits = min_bits
                threshold = 1 << code_bits

                codeword = reader.read(code_bits)

                if codeword is None:
                    raise ValueError("Corrupted file: unexpected end after RESET")

                if codeword == EOF_CODE:
                    break

                # Decode and continue, since no new dictionary entry after a reset
                prev = dictionary[codeword]
                out.write(prev.encode('latin-1'))
                continue

            if codeword in dictionary:
                current = dictionary[codeword]
            elif codeword == next_code:
                current = prev + prev[0]
            else:
                raise ValueError(f"Invalid codeword: {codeword}")

            out.write(current.encode('latin-1'))

            if next_code < max_size:
                dictionary[next_code] = prev + current[0]
                next_code += 1

            prev = current

    reader.close()
    print(f"Decompressed: {input_file} -> {output_file}")

# ============================================================================
# COMMAND-LINE INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='LZW compression (reset mode)')
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