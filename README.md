# LZW Compression with Dictionary Management

A comprehensive implementation of LZW (Lempel-Ziv-Welch) compression in Python, with various dictionary management strategies.

## To-do
 - [ ] Eventually se if we're able to create LRU and LFU encoding by directing mirroring encoder logic somehow (this will make sense once you read the rest of the readme)

## Table of Contents

- [What is LZW Compression?](#what-is-lzw-compression)
- [Dictionary Management](#dictionary-management)
- [Implemented Strategies](#implemented-strategies)
  - [Freeze (Baseline)](#1-freeze-baseline)
  - [Reset](#2-reset)
  - [LRU (Least Recently Used)](#3-lru-least-recently-used)
  - [LFU (Least Frequently Used)](#4-lfu-least-frequently-used)
- [Performance Comparisons](#performance-comparisons)
- [Usage](#usage)
- [File Formats](#file-formats)

## What is LZW Compression?

**LZW (Lempel-Ziv-Welch)** is a dictionary-based compression algorithm that works by replacing repeated sequences of data with shorter codes. It's the algorithm behind GIF images, Unix's `compress` utility, and PDF compression.

### How It Works

**Compression:**
1. Initialize a dictionary with all single characters (the alphabet)
2. Read input character by character, building up phrases
3. When you find a phrase not in the dictionary:
   - Output the code for the longest matching prefix
   - Add the new phrase to the dictionary with a new code
4. Continue until the entire input is processed

**Decompression:**
1. Initialize the same dictionary
2. Read codes from the compressed file
3. Output the phrase for each code
4. Reconstruct new dictionary entries by watching the pattern of codes

LZW uses **variable-width codes** to maximize efficiency. Each code output during compression starts at `min_bits` (e.g., 9 bits for ASCII) and grows when needed up to `max_bits` (e.g., 16 bits = 65,536 max codes).

### Example

Input: `"ababab"` (6 characters)
 
| Step | Read | Current Phrase | Action | Output | New Entry |
|------|------|----------------|--------|--------|-----------|
| 1 | a | "a" | Match found | — | — |
| 2 | b | "ab" | No match! | **0** (a) | 2:"ab" |
| 3 | a | "ba" | No match! | **1** (b) | 3:"ba" |
| 4 | b | "ab" | Match found! | — | — |
| 5 | a | "aba" | No match! | **2** (ab) | 4:"aba" |
| 6 | b | "ab" | Match found! | — | — |
| EOF | — | "ab" | End of input | **2** (ab) | — |
 
**Compressed output:** `[0, 1, 2, 2]` (4 codes instead of 6 characters)
 
**Decompression verification:**
- Code 0 → "a", output "a", add 2:"a"+"b"[0]="ab"
- Code 1 → "b", output "b", add 3:"b"+"a"[0]="ba"
- Code 2 → "ab", output "ab", add 4:"ab"+"a"[0]="aba"
- Code 2 → "ab", output "ab"
 
**Final output:** "a" + "b" + "ab" + "ab" = **"ababab"** ✓

## Dictionary Management

The core challenge in LZW is: **What happens when the dictionary fills up?**

Once you reach `2^max_bits` codes (e.g., 65,536 codes for 16-bit max), you can't add more entries. Different strategies for handling this situation produce dramatically different compression ratios and performance characteristics.

### Why Dictionary Management Matters

**File characteristics vary:**
- Some files have consistent patterns (encyclopedias, source code)
- Others have shifting contexts (concatenated documents, log files)
- Some have high entropy (encrypted data, random noise)

**No single strategy is optimal for all files.** The best approach depends on:
- **Pattern stability:** Do patterns repeat throughout the file?
- **Context shifts:** Does the file have distinct sections with different vocabularies?
- **Locality:** Are recent patterns more likely to repeat than old ones?

I have here 4 strategies of dictionary management to see how this works. 

In `/Annotated/`, we have all 4 strategies with detailed comments and documentation, as well as the evolution of me figuring out how to do the LRU strategy (which will become the base of the LFU strategy). The LRU versions 2 and 2.1 are the final LRU versions which are most efficient that I could get it. The LFU strategy is based on the LRU version 2.1. In `/Clean/`, we have all 4 strategies without any comments or documentation. Only the 2.1 version of LRU is included.

## Implemented Strategies

### 1. Freeze (Baseline)

**Strategy:** When the dictionary fills up, stop adding new entries. Continue compressing with the existing dictionary.

**How It Works:**
```python
if next_code < max_codes:
    dictionary[current + char] = next_code
    next_code += 1
# else: do nothing, dictionary is frozen
```

**Pros:**
- Simplest implementation
- Minimal overhead
- Deterministic behavior

**Cons:**
- Can't adapt to new patterns after dictionary fills
- Poor performance on files with shifting contexts
- Compression ratio degrades over long files

**Best For:** Files with stable patterns established early (uniform data, simple repeated structures)

### 2. Reset

**Strategy:** When the dictionary fills up, clear it and reinitialize with just the alphabet. Start learning patterns from scratch.

**How It Works:**

When `next_code` reaches `max_codes`, the encoder:
1. Sends a special **RESET_CODE** signal
2. Clears the dictionary
3. Reinitializes with only the alphabet
4. Resets code width to `min_bits`
5. Continues compressing with fresh dictionary

The decoder mirrors this behavior:
- When it reads **RESET_CODE**, it performs the same reset
- Both encoder and decoder stay synchronized

**Reset Code Allocation:**

The RESET_CODE is reserved at the beginning:
```python
# Reserve special codes
EOF_CODE = alphabet_size      # e.g., 256 for extended ASCII
RESET_CODE = alphabet_size + 1  # e.g., 257
next_code = alphabet_size + 2   # Start adding at 258
```

**RESET_CODE Format in Compressed File:**
```
[...normal codes...][RESET_CODE][...codes with fresh dictionary...]
```

When `next_code` reaches `max_codes`:
```python
if next_code >= max_codes:
    writer.write(RESET_CODE, code_bits)
    # Reinitialize dictionary
    dictionary = {char: idx for idx, char in enumerate(alphabet)}
    next_code = alphabet_size + 2  # Skip EOF and RESET_CODE
    code_bits = min_bits
```

**Pros:**
- Adapts to context shifts (new sections of file with different patterns)
- Prevents "stale" entries from occupying space
- Works well on concatenated files with distinct sections

**Cons:**
- Loses all learned patterns on reset (sudden compression ratio drop)
- Reset overhead (RESET_CODE signal + relearning period)
- Poor on files with globally common patterns

**Best For:** Files with distinct phases (e.g., concatenated logs, multi-part documents, files with chapter boundaries)

### 3. LRU (Least Recently Used)

**Strategy:** When the dictionary fills up, evict the least recently used entry and reuse its code for the new pattern.

Why LRU? A lot of times, recent patterns are more likely to repeat than old ones (principle of **locality of reference**). By keeping recently-used entries, the dictionary stays adapted to the current context.

LRU uses **two data structures** for O(1) operations:

1. **Doubly-linked list (priority queue)** for LRU ordering (O(1) move-to-front) with sentinel head/tail nodes as placeholders to eliminate any edge cases we need to deal with
2. **HashMap** for O(1) code lookup in the queue

```
Structure Example:

  Priority Queue: [HEAD] ↔ ["xyz"] ↔ ["abc"] ↔ [TAIL]
  HashMap: {"xyz" → Node["xyz"], "abc" → Node["abc"]}
```

However, implementing this has a core challenge, because the LZW algorithm has the encoder and decoder be fundamentally out of sync (the decoder is one step behind). This means the eviction procedure will cause issues. 

**The Problem:**
1. Encoder evicts code `A` replacing it with `C` (replaces entry "abc" with new entry "xyz")
2. Encoder then outputs code `C` with its new value sometime later (meaning "xyz")
3. Decoder still thinks code `C` means "abc" → **DESYNC!**

If one were to not account for this problem and merely mirror encoder LRU logic in the decoder, the moment a situation like this happens, the decoder fails and decompression results in corrupted data. This is a hard issue to solve, and initially I had made little progress. However, I eventually got a version working (naive implementation) where I transmitted an EVICT_SIGNAL with the **full dictionary entry** written out behind it **at every eviction**, in order to manually sync the decoder's dictionary. This has extreme overhead, and will not "compress" anything, but showed as a proof-of-concept that manual synchronization will work. Then, I worked on optimizing this strategy as follows.

#### LRU Optimization 1: Evict-Then-Use Pattern Detection

**Key Insight:** Not all evictions need a signal!

The decoder can evict and replace the evicted code with something bogus all it wants (or just not do anything and freeze, even). If it doesn't need to use it, it doesn't matter. We only need EVICT_SIGNAL in the evict-then-use pattern. This is surprisingly rare (~10-30% of evictions).

The EVICT_SIGNAL has the following format:
```
[EVICT_SIGNAL][code][entry length][char1][char2]...[charN][code again]
```

**Bit Cost:**
- `code_bits` (EVICT_SIGNAL marker)
- `code_bits` (which code was evicted)
- 16 bits (entry length)
- 8 × [# of characters] bits (entry characters)
- `code_bits` (repeat the code to actually emit it)

**Example:** 9-bit codes, 10-char entry = 9+9+16+80+9 = **123 bits**

This allows us to achive significant reduction of overhead:
- Naive approach: Signal on every eviction (~100% evictions)
- Optimization 1: Signal only on evict-then-use (~10-30% evictions)
- **Result: 70-90% reduction in signals!**
- Benchmarks show **55-85% smaller output** vs naive across typical files

While this dramatically reduces signal frequency, each signal is still large (123 bits for 10-char entry). Overhead is noticeable on files with high eviction rates. How can we do better?

#### LRU Optimization 2: Output History with Offset+Suffix Encoding

**Key Insight:** We can compress the EVICT_SIGNAL itself!

When the encoder needs to send an evicted entry, that entry is very likely to be **recently output**. We can reference it from recent history instead of sending the full string.

**Output History:**
- Maintain a circular buffer of the last **255 outputs**
- When sending EVICT_SIGNAL, find the prefix in history
- Send **offset + suffix** instead of full entry

**Example:**<br>
Entry: `"programming"`<br>
Prefix: `"programmin"` (last output 5 steps ago)<br>
Suffix: `"g"`

**Old format:** `[EVICT_SIGNAL][code][11]['p']['r']['o']['g']['r']['a']['m']['m']['i']['n']['g']`<br>
**New format:** `[EVICT_SIGNAL][code][5]['g']`

**Bit Cost:**
- Old: 9+9+16+88 = **122 bits** (9-bit codes, 11-char entry)
- New: 9+9+8+8 = **34 bits**
- **Savings: 72% reduction!**

**Fallback:** If prefix not in recent history, send full entry with `offset=0` as a signal. As it turns out, it is **exceedingly rare** for a prefix to not be in recent history (outside of the 255 entry buffer). During testing, I unironically have **not encountered a single case where this has happenned**. Most files have maximum offsets of around ~200-220.

Overall, the format would be:
```
Compact: [EVICT_SIGNAL][code][offset (1-255)][suffix][code again]
Fallback: [EVICT_SIGNAL][code][0][entry length][full entry][code again]
```

There are two approaches to implementing this new optimization:

**Linear Search Version:**
- Searches output history backwards: **O(255×L) lookup**
- Memory overhead: ~0 KB (just the circular buffer)
- 3-10% slower overall compression (prefix lookup is small fraction of total time)
- Best for: Memory-constrained embedded systems, ??? idk

**HashMap Version:**
- Maintains `string_to_idx` HashMap for **O(1) prefix lookup**
- Memory overhead: ~4 KB (~8.7% for typical files)
- **3,800× faster** prefix lookup vs linear search
- Best for: General use, large files

Even with these optimizations, overall, LRU tbh isn't that good.

**Pros:**
- Adapts to changing contexts (keeps recent patterns)
- Excellent for files with locality of reference
- O(1) eviction operations (no scanning required)
- Simple decoder (no eviction tracking needed)
 
**Cons:**
- Doesn't preserve globally-common patterns from early in file
- EVICT_SIGNAL overhead (~0.1-1% depending on optimization version)
- More complex than Freeze/Reset
 
**Best For:** Files with temporal locality (source code, natural language text, logs with repeating recent patterns)

### 4. LFU (Least Frequently Used)

**Strategy:** When the dictionary fills up, evict the entry that has been output the **fewest times**. This preserves globally common patterns.

**Why LFU?** Globally frequent patterns stay in the dictionary regardless of when they were last used. This works well on files with stable, recurring vocabularies (encyclopedias, documentation, structured data).

LFU uses **three data structures** for O(1) operations:

1. **HashMap #1**: HashMap mapping entries to nodes (O(1) lookup)
2. **HashMap #2**: HashMap mapping frequencies to doubly-linked lists (O(1) bucket access)
3. **Integer**: Tracks the minimum frequency for fast LFU eviction (O(1) find)

```
Structure Example:

  HashMap #1: {"ab" → Node["ab"], "xyz" → Node["xyz"], "ca" → Node["ca"]}
  HashMap #2:
    freq=1: [HEAD] ↔ ["xyz"] ↔ ["ca"] ↔ [TAIL]
    freq=2: [HEAD] ↔ ["ab"] ↔ [TAIL]

  Integer: 1
```

**How it works:**
- New entries start at freq=1 (most recently used position in freq=1 list)
- `use(entry)`: Move from freq=N to freq=N+1, update minimum frequency if needed (~20-25 operations)
- `find_lfu()`: Return LRU entry in minimum frequency bucket (constant time by tracking the minimum frequency)
- **LRU tie-breaking:** Among entries with same frequency, evict the least recently used (in the above example, we would evict "ca")

**Why LRU tie-breaking?** Preserves recent patterns even among rarely-used entries.

LFU uses the **same EVICT_SIGNAL mechanism** as LRU:

**Eviction:** Find LFU entry (e.g. freq=1, LRU in bucket) → remove → reuse its code → track in evicted_codes → send EVICT_SIGNAL when outputting reused code

Hence it shares these components with LRU:
- Output history buffer (255 entries) + string_to_idx HashMap
- evicted codes tracker for evict-then-use synchronization
- Compact offset+suffix encoding (fallback to full entry if prefix not in history)

However, it ofc evicts based on frequency (LFU+LRU tie-break) instead of recency (LRU only). We note that this LFU implementation is based on the optimized version 2.1 of LRU which uses a hashmap for efficient prefix lookup.

**Pros:**
- Preserves globally common patterns
- Works well on files with stable vocabularies
- O(1) eviction with frequency buckets
- Same compact EVICT_SIGNAL optimization as LRU

**Cons:**
- Can keep stale entries too long in shifting contexts
- Higher memory overhead (frequency tracking + multiple lists)
- More complex than LRU (requires mininum frequency maintenance)
- **2-3× slower than LRU** due to constant factor overhead

**Best For:** Files with globally-repeated patterns (documentation, structured logs, encyclopedias)

**Why is LFU slower than LRU?**

While both LRU and LFU have O(1) `use()` operations, **constant factors matter**:
- **LRU `use()`**: ~8 operations (remove from list + add to head)
- **LFU `use()`**: ~20-25 operations (remove from freq=N list + update min_freq + add to freq=N+1 list + dict lookups)

With 500k outputs, this becomes:
- LRU: 500k × 8 = **4M operations**
- LFU: 500k × 20 = **10M operations** → **2.5× slower**

This overhead applies to **every output**, not just evictions, which is why LFU is consistently 1.5-2.5× slower than LRU across all file types (even when compression ratios are identical).

## Performance Comparisons

### Test Files

| File | Size | Description |
|------|------|-------------|
| `ab_repeat_250k` | 488 KB | Highly repetitive 'ab' pattern (250k repetitions) |
| `ab_random_500k` | 488 KB | Random a/b characters (500k bytes) |
| `code.txt` | 68 KB | Java source code |
| `code2.txt` | 54 KB | Additional source code |
| `medium.txt` | 24 KB | Medium text file |
| `large.txt` | 1.15 MB | Large text file |
| `bmps.tar` | 1.05 MB | BMP image archive |
| `all.tar` | 2.89 MB | Mixed file archive |
| `wacky.bmp` | 900 KB | BMP image with high compression potential |

### Compression Ratio Comparison

Comprehensive benchmarks comparing all implementations across diverse file types and dictionary sizes. We note that LRU-v2 and LRU-v2.1 compress into the same size because they have the same compression algorithm, just different runtime. Hence, only v2 is included in these benchmarks.

#### **AB Alphabet Tests (Small Dictionary)**

##### **Random 500KB a/b characters**

| max-bits | Freeze | Reset | LFU | LRU-v1 | LRU-v2 |
|----------|--------|-------|-----|--------|--------|
| 3 | **91.58 KB (18.76%)** | 155.82 KB (31.91%) | 138.37 KB (28.34%) | 712.75 KB (145.97%) | 439.72 KB (90.05%) |
| 4 | **86.61 KB (17.74%)** | 130.68 KB (26.76%) | 100.83 KB (20.65%) | 744.01 KB (152.37%) | 429.13 KB (87.89%) |
| 5 | **80.99 KB (16.59%)** | 115.02 KB (23.56%) | 94.39 KB (19.33%) | 703.23 KB (144.02%) | 383.20 KB (78.48%) |
| 6 | **78.15 KB (16.01%)** | 104.37 KB (21.37%) | 83.81 KB (17.16%) | 660.38 KB (135.25%) | 342.17 KB (70.08%) |

**Winner:** Freeze dominates on random data (18-28% smaller than next best)

##### **Repetitive 250k 'ab' pattern**

| max-bits | Freeze | Reset | LFU | LRU-v1 | LRU-v2 |
|----------|--------|-------|-----|--------|--------|
| 3 | **45.78 KB (9.38%)** | 122.08 KB (25.00%) | 91.56 KB (18.75%) | 926.96 KB (189.84%) | 499.72 KB (102.34%) |
| 4 | **30.53 KB (6.25%)** | 69.76 KB (14.29%) | 61.04 KB (12.50%) | 822.50 KB (168.45%) | 349.29 KB (71.54%) |
| 5 | **19.09 KB (3.91%)** | 40.70 KB (8.34%) | 38.16 KB (7.81%) | 697.11 KB (142.77%) | 212.96 KB (43.62%) |
| 6 | **11.47 KB (2.35%)** | 23.64 KB (4.84%) | 22.90 KB (4.69%) | 610.66 KB (125.06%) | 124.33 KB (25.46%) |

**Winner:** Freeze dominates (2-10× better than eviction strategies!)

#### **Extended ASCII Tests (Standard Dictionary)**

##### **large.txt (1.15 MB) - Large diverse text**

| max-bits | Freeze | Reset | LFU | LRU-v1 | LRU-v2 |
|----------|--------|-------|-----|--------|--------|
| 9 | 783.08 KB (66.66%) | 899.83 KB (76.60%) | **708.87 KB (60.34%)** | 2020.96 KB (172.03%) | 1568.66 KB (133.53%) |
| 10 | 675.02 KB (57.46%) | 788.21 KB (67.09%) | **626.44 KB (53.32%)** | 1895.98 KB (161.39%) | 1551.25 KB (132.05%) |
| 11 | 626.02 KB (53.29%) | 723.28 KB (61.57%) | **589.15 KB (50.15%)** | 1823.58 KB (155.23%) | 1626.97 KB (138.49%) |
| 12 | 585.61 KB (49.85%) | 673.73 KB (57.35%) | **562.66 KB (47.90%)** | 1750.52 KB (149.01%) | 1668.43 KB (142.02%) |

**Winner:** LFU excels on large files with stable vocabularies (4-10% better than Freeze)

##### **bmps.tar (1.05 MB) - Image archive**

| max-bits | Freeze | Reset | LFU | LRU-v1 | LRU-v2 |
|----------|--------|-------|-----|--------|--------|
| 9 | 699.39 KB (64.76%) | **89.64 KB (8.30%)** | 266.97 KB (24.72%) | 1189.85 KB (110.17%) | 206.56 KB (19.13%) |
| 10 | 763.45 KB (70.69%) | **76.34 KB (7.07%)** | 257.28 KB (23.82%) | 1147.47 KB (106.25%) | 191.12 KB (17.70%) |
| 11 | 833.94 KB (77.22%) | **73.03 KB (6.76%)** | 153.43 KB (14.21%) | 1131.70 KB (104.79%) | 193.81 KB (17.95%) |
| 12 | 903.65 KB (83.67%) | **73.68 KB (6.82%)** | 158.26 KB (14.65%) | 1107.00 KB (102.50%) | 194.97 KB (18.05%) |

**Winner:** Reset dramatically wins (7-12× better than Freeze!) due to context shifts in archive

##### **wacky.bmp (900 KB) - Highly compressible image**

| max-bits | Freeze | Reset | LFU | LRU-v1 | LRU-v2 |
|----------|--------|-------|-----|--------|--------|
| 9 | 14.53 KB (1.61%) | **11.49 KB (1.28%)** | 18.44 KB (2.05%) | 904.84 KB (100.53%) | 44.83 KB (4.98%) |
| 10 | 13.08 KB (1.45%) | **6.37 KB (0.71%)** | 14.66 KB (1.63%) | 633.88 KB (70.43%) | 21.84 KB (2.43%) |
| 11 | **4.23 KB (0.47%)** | 4.95 KB (0.55%) | 5.48 KB (0.61%) | 207.78 KB (23.09%) | 11.32 KB (1.26%) |
| 12 | **4.46 KB (0.49%)** | **4.46 KB (0.49%)** | **4.46 KB (0.49%)** | **4.46 KB (0.49%)** | **4.46 KB (0.49%)** |

**Winner:** All strategies converge at max-bits=12 (dictionary large enough to capture all patterns)

**Key Insights:**
- **Freeze dominates** on repetitive patterns, random data, uniform text
- **Reset excels** on files with context shifts (bmps.tar: 8× better than Freeze)
- **LRU-v1 performs poorly** across all tests as expected (file expansion!)
- **LRU-v2 is ok** on mixed archives but mid overall (bmps.tar: 3.4× better than Freeze)
- **LFU excels** on large files with stable, globally-repeated patterns (large.txt)
- **Higher max-bits** reduces eviction pressure, narrowing gaps between strategies

### Compression Speed Comparison

Systematic speed benchmarks across all implementations (max-bits=9, averaged over 3 runs):

| File | Size | Freeze | Reset | LFU | LRU-v1 | LRU-v2 | LRU-v2.1 |
|------|------|--------|-------|-----|--------|--------|----------|
| **ab_repeat_250k** | 488 KB | **0.20s** | **0.20s** | 0.22s | 0.40s | 0.22s | 0.23s |
| **ab_random_500k** | 488 KB | **0.21s** | 0.23s | 0.34s | 0.53s | 0.61s | 0.45s |
| **code.txt** | 68 KB | **0.12s** | **0.12s** | 0.18s | 0.19s | 0.21s | 0.19s |
| **large.txt** | 1.15 MB | **0.75s** | 0.90s | 1.85s | 1.97s | 2.35s | 2.19s |
| **bmps.tar** | 1.05 MB | 0.72s | **0.38s** | 0.76s | 0.98s | 0.60s | 0.58s |
| **all.tar** | 2.89 MB | **1.81s** | **1.81s** | 4.93s | 4.02s | 4.20s | 3.76s |
| **wacky.bmp** | 900 KB | 0.29s | **0.28s** | 0.35s | 0.74s | 0.34s | 0.36s |

**Speed Rankings (Fastest → Slowest):**
1. **Freeze/Reset** - Fastest overall (0.12s - 1.81s across tests)
2. **LRU-v2.1 vs LRU-v2:** v2.1 is 7-34% faster (better optimization)
3. **LRU-v1** - idk just here never use this shit ass one lol
4. **LFU** - 2-3× slower than Freeze due to frequency tracking overhead

**Speed vs Compression Trade-off:**
- **Freeze:** Fastest but poor on archives (bmps.tar: 0.72s, 699 KB)
- **Reset:** Fast AND best on archives (bmps.tar: 0.38s, 90 KB)
- **LFU:** Slow but best on large.txt (1.85s, 709 KB vs Freeze 0.75s, 783 KB)
- **LRU-v2.1:** Middle ground (mid on bmps.tar: 0.58s, 207 KB)

### Memory Usage (Worst Case Analysis)

| Strategy | Dictionary | Metadata | Total Overhead |
|----------|-----------|----------|----------------|
| Freeze | ~512 KB (65K entries × 8 bytes) | 0 KB | ~512 KB |
| Reset | ~512 KB | 0 KB | ~512 KB |
| LRU-v2 | ~512 KB | ~512 KB (list) | ~1 MB |
| LRU-v2.1 | ~512 KB | ~516 KB (list + 4 KB hash) | ~1 MB |
| LFU | ~512 KB | ~640 KB (freq buckets + lists) | ~1.15 MB |

**Observations:**
- **Freeze/Reset** use least memory (no eviction tracking overhead)
- **LRU strategies** use ~2× memory of Freeze/Reset (doubly-linked list overhead)
- **LFU** uses most memory (~2.25× Freeze) due to frequency buckets + multiple linked lists
- **LRU-v2 vs v2.1:** v2.1 uses slightly more memory due to HashMap tracking of prefixes
- All strategies practical for modern systems (< 1.2 MB total)

### LRU v2.1 vs LFU Comparison

Battle of mid.

#### **Compression Ratio Comparison**

| Test Type | File | max-bits | LRU v2.1 | LFU | Winner |
|-----------|------|----------|----------|-----|--------|
| **Random (500k a/b)** | | 3 | 439.72 KB (90.05%) | **138.37 KB (28.34%)** | **LFU** (68.5% better) |
| | | 4 | 429.13 KB (87.89%) | **100.83 KB (20.65%)** | **LFU** (76.5% better) |
| | | 5 | 383.20 KB (78.48%) | **94.39 KB (19.33%)** | **LFU** (75.4% better) |
| | | 6 | 342.17 KB (70.08%) | **83.81 KB (17.16%)** | **LFU** (75.5% better) |
| **Repetitive (250k 'ab')** | | 3 | 499.72 KB (102.34%) | **91.56 KB (18.75%)** | **LFU** (81.7% better) |
| | | 4 | 349.29 KB (71.54%) | **61.04 KB (12.50%)** | **LFU** (82.5% better) |
| | | 5 | 212.96 KB (43.62%) | **38.16 KB (7.81%)** | **LFU** (82.1% better) |
| | | 6 | 124.33 KB (25.46%) | **22.90 KB (4.69%)** | **LFU** (81.6% better) |
| **Large Diverse** | large.txt (1.15 MB) | 9 | 1568.66 KB (133.53%) | **708.87 KB (60.34%)** | **LFU** (54.8% better) |
| | | 10 | 1551.25 KB (132.05%) | **626.44 KB (53.32%)** | **LFU** (59.6% better) |
| | | 11 | 1626.97 KB (138.49%) | **589.15 KB (50.15%)** | **LFU** (63.8% better) |
| | | 12 | 1668.43 KB (142.02%) | **562.66 KB (47.90%)** | **LFU** (66.3% better) |
| **Code Files** | code.txt (68 KB) | 9 | 90.17 KB (132.82%) | **43.37 KB (63.88%)** | **LFU** (51.9% better) |
| | | 12 | 77.10 KB (113.57%) | **36.34 KB (53.53%)** | **LFU** (52.9% better) |
| | code2.txt (54 KB) | 9 | 68.31 KB (126.78%) | **30.97 KB (57.47%)** | **LFU** (54.7% better) |
| | | 12 | 55.80 KB (103.57%) | **31.17 KB (57.86%)** | **LFU** (44.1% better) |

**Summary:** LRU is absolute shittery

#### **Speed Comparison (max-bits=9, averaged over 3 runs)** - i literally just extracted this from the speed table prior lol

| File | LRU v2.1 Time | LFU Time | Faster |
|------|---------------|----------|--------|
| **ab_repeat_250k** | 0.23s | **0.22s** | LFU 1.05× |
| **ab_random_500k** | 0.45s | **0.34s** | **LFU 1.32× faster** |
| **code.txt** | 0.19s | **0.18s** | LFU 1.06× |
| **large.txt** | 2.19s | **1.85s** | **LFU 1.18× faster** |
| **bmps.tar** | **0.58s** | 0.76s | **LRU 1.31× faster** |

**Summary:** LRU also pretty shittery ngl only won 1 benchmark test

### LFU vs Freeze Comparison

Comparing complexity (LFU) vs simplicity (Freeze).

#### **Compression Ratio Comparison**

| Test Type | File | max-bits | Freeze | LFU | Winner |
|-----------|------|----------|--------|-----|--------|
| **Random (500k a/b)** | | 3 | **91.58 KB (18.76%)** | 138.37 KB (28.34%) | **Freeze** (33.8% better) |
| | | 4 | **86.61 KB (17.74%)** | 100.83 KB (20.65%) | **Freeze** (14.1% better) |
| | | 5 | **80.99 KB (16.59%)** | 94.39 KB (19.33%) | **Freeze** (14.2% better) |
| | | 6 | **78.15 KB (16.01%)** | 83.81 KB (17.16%) | **Freeze** (6.7% better) |
| **Repetitive (250k 'ab')** | | 3 | **45.78 KB (9.38%)** | 91.56 KB (18.75%) | **Freeze** (50.0% better) |
| | | 4 | **30.53 KB (6.25%)** | 61.04 KB (12.50%) | **Freeze** (50.0% better) |
| | | 5 | **19.09 KB (3.91%)** | 38.16 KB (7.81%) | **Freeze** (50.0% better) |
| | | 6 | **11.47 KB (2.35%)** | 22.90 KB (4.69%) | **Freeze** (49.9% better) |
| **Large Diverse** | large.txt (1.15 MB) | 9 | 783.08 KB (66.66%) | **708.87 KB (60.34%)** | **LFU** (9.5% better) |
| | | 10 | 675.02 KB (57.46%) | **626.44 KB (53.32%)** | **LFU** (7.2% better) |
| | | 11 | 626.02 KB (53.29%) | **589.15 KB (50.15%)** | **LFU** (5.9% better) |
| | | 12 | 585.61 KB (49.85%) | **562.66 KB (47.90%)** | **LFU** (3.9% better) |
| **Code Files** | code.txt (68 KB) | 9 | 44.83 KB (66.03%) | **43.37 KB (63.88%)** | **LFU** (3.3% better) |
| | | 12 | **30.38 KB (44.76%)** | 36.34 KB (53.53%) | **Freeze** (16.4% better) |
| | code2.txt (54 KB) | 9 | 35.69 KB (66.24%) | **30.97 KB (57.47%)** | **LFU** (13.2% better) |
| | | 12 | **23.47 KB (43.56%)** | 31.17 KB (57.86%) | **Freeze** (24.7% better) |

**Summary:** Freeze wins 12/16 tests (75%), LFU wins 4/16 tests (25%). On practical data (extendedascii), LFU is pretty optimal.

#### **Speed Comparison (max-bits=9, averaged over 3 runs)** - again just copied from the speed table prior

| File | Freeze Time | LFU Time | Faster |
|------|-------------|----------|--------|
| **ab_repeat_250k** | **0.20s** | 0.22s | **Freeze 1.10× faster** |
| **ab_random_500k** | **0.21s** | 0.34s | **Freeze 1.62× faster** |
| **code.txt** | **0.12s** | 0.18s | **Freeze 1.50× faster** |
| **large.txt** | **0.75s** | 1.85s | **Freeze 2.47× faster** |
| **bmps.tar** | **0.72s** | 0.76s | Freeze 1.06× faster |

**Summary:** Freeze faster on 5/5 tests (100%)! Average 1.75× faster.

**Key Findings:**
- **Freeze dominates** on random and repetitive data (7-50% better compression!)
- **LFU wins** only on large files with stable vocabularies (large.txt: 9.5% better)
- **Freeze is always faster** (1.06-2.47× faster) - no eviction overhead
- **Surprising result:** Freeze's simplicity beats LFU's complexity on 75% of tests
- **LFU only worthwhile** for specific use cases (encyclopedias, large docs with repeated patterns)

### Summary: When to Use Each Strategy

| Strategy | Best For | Compression Ratio | Speed | Memory |
|----------|----------|-------------------|-------|--------|
| **Freeze** | Repetitive patterns, uniform text, random data | **Excellent** (wins on random/repetitive data) | **Fastest** (1.5-2.5× faster than eviction strategies) | **Lowest** (~512 KB) |
| **Reset** | Archives, multi-section files, context shifts | **Best for archives** (8-77× better than Freeze on bmps.tar/wacky.bmp) | **Very Fast** (same as Freeze) | **Lowest** (~512 KB) |
| **LRU-v2.1** | Meh I guess mixed archives of some sort | **Second-best for archives** (3.4× better than Freeze on bmps.tar, beats LFU) | Medium-Fast | Medium (~1 MB) |
| **LRU-v2** | (Same as v2.1, slightly slower) | Same as LRU-v2.1 | Medium-Slow | Medium (~1 MB) |
| **LRU-v1** | lol | Very Poor (file expansion 100-190%) | Medium-Fast | Medium (~1 MB) |
| **LFU** | Large text files with stable vocabularies | **Best for large uniform text** (5-10% better than Freeze on large.txt) | Medium (2× slower than Freeze) | High (~1.15 MB) |

**Conclusions:**
1. **For repetitive/uniform data:** Use **Freeze** (fastest, excellent compression)
    >**Repetitive (ab_repeat):** Freeze (2.4 KB) > Reset (4.4 KB) > LFU (4.3 KB) > LRU (22 KB)
2. **For archives (tar, zip, mixed files):** Use **Reset** (best) or **LRU-v2.1** (second-best, 3.4× better than Freeze)
    >**Archives (bmps.tar):** Reset (90 KB) > LRU-v2.1 (207 KB) > LFU (267 KB) > Freeze (699 KB)
3. **For large text files:** Use **LFU** (5-10% better than Freeze on large.txt)
    >**Large text (large.txt):** LFU (709 KB) > Freeze (783 KB) > Reset (900 KB) > LRU (1569 KB)
4. LRU is generally a shitty strategy, never comes up on top in any of the test cases, and is too complicated for what it's worth. Not worth using.
5. Just use Freeze or Reset on most things. Freeze generally is better than LFU especially if the dictionary is large (we never even tested standard `maxW`=16, which won't really fill up in most practical cases). Plus, Freeze and Reset are incredibly simple to implement compared to LRU or LFU.

## Usage

No external libraries needed, so no need to pip install :)

### Compress a File

```bash
python [LZW-strategy].py compress --alphabet [alphabet] --min-bits [num] --max-bits [num] [input file] [output file]
```
### Decompress a File

```bash
python [LZW-strategy].py decompress [input file] [output file]
```

### Currently Available Alphabets

| Alphabet | Size | Description |
|----------|------|-------------|
| `ascii` | 128 | Standard ASCII (0-127) |
| `extendedascii` | 256 | Extended ASCII (0-255) |
| `ab` | 2 | Binary alphabet (for testing) |

Add custom alphabets in the `ALPHABETS` dictionary at the top of each file.

### Recommended Parameters

**For binary/text files:**
```bash
--alphabet extendedascii --min-bits 9 --max-bits 16
```
- 9 bits = 512 codes (min for 256 Extended ASCII + special codes)
- 16 bits = 65,536 codes (good balance for most files)

**For testing/debugging:**
```bash
--alphabet ab --min-bits 3 --max-bits 3
```
- Small alphabet makes behavior easier to trace
- Quick dictionary fill for testing eviction logic

## File Formats

### Compressed File Structure

```
┌─────────────────────────────────────────────────────────────┐
│ HEADER                                                      │
├─────────────────────────────────────────────────────────────┤
│ min_bits (8 bits)                                           │
│ max_bits (8 bits)                                           │
│ alphabet_size (16 bits)                                     │
│ alphabet[0] (8 bits)                                        │
│ alphabet[1] (8 bits)                                        │
│ ...                                                         │
│ alphabet[N-1] (8 bits)                                      │
├─────────────────────────────────────────────────────────────┤
│ COMPRESSED DATA                                             │
├─────────────────────────────────────────────────────────────┤
│ code[0] (min_bits to max_bits, variable)                    │
│ code[1] (min_bits to max_bits, variable)                    │
│ ...                                                         │
│ [RESET_CODE] (only in Reset strategy)                       │
│ [EVICT_SIGNAL][code][data] (only in LRU/LRU strategies)     │
│ ...                                                         │
│ EOF_CODE (code_bits at end)                                 │
└─────────────────────────────────────────────────────────────┘
```

### Special Codes

| Code | Value | Purpose |
|------|-------|---------|
| Alphabet | 0 to N-1 | Single characters |
| EOF_CODE | N | End of file marker |
| RESET_CODE | N+1 | Dictionary reset signal (Reset only) |
| EVICT_SIGNAL | 2^max_bits - 1 | Eviction sync (LRU and LFU) |
| Regular codes | N+2 to 2^max_bits - 1 (or - 2 for LRU/LFU) | Dictionary entries |

## Implementation Notes

### Code Structure

Each implementation follows this structure:

```python
# 1. Predefined alphabets
ALPHABETS = {'ascii': [...], 'extendedascii': [...], 'ab': [...]}

# 2. Bit-level I/O classes
class BitWriter:  # Packs variable-width integers into bytes
class BitReader:  # Unpacks bytes into variable-width integers

# 3. Strategy-specific data structures
class LRUTracker:  # Only in LRU implementations
class LFUTracker:  # Only in LFU implementations

# 4. Compression function
def compress(input_file, output_file, alphabet_name, min_bits, max_bits):
    # - Initialize dictionary with alphabet
    # - Read input, match longest phrases
    # - Add new phrases to dictionary
    # - Apply eviction strategy when full
    # - Write codes to output

# 5. Decompression function
def decompress(input_file, output_file):
    # - Read header to get parameters
    # - Initialize dictionary
    # - Read codes, output phrases
    # - Reconstruct dictionary entries
    # - Handle special signals (RESET_CODE, EVICT_SIGNAL)

# 6. CLI argument parsing
if __name__ == '__main__':
    # argparse setup for compress/decompress commands
```

### Testing

Testing is generally done by round-trip difference checking:
```bash
# Compress
python LZW-Script.py compress --alphabet extendedascii --min-bits 9 --max-bits 9 input.txt output.lzw

# Decompress
python LZW-Script.py decompress output.lzw restored.txt

# Verify
diff input.txt restored.txt  # Should be identical
```
Minimal `max-bits` allow eviction policies to actually do their job so we can observe their behavior. This of course relies on the fact that the eviction strategies and/or the internal logic of compress/decompress are implemented fully correctly, which hopefully they are. I did a lot of debugging and stuff, but I'm not perfect so there may be some errors still. I hope not though.

## Possible Stuff?

 - There's a lot of other cache management strategies, any of those are potential eviction strategies for the dictionary (i.e. FIFO, RR, TTL, totally MRU, &c).
 - There can be hybrid strategies, like adaptive switching between LFU + LRU, or [adaptive switching between removing unused entries (NOT LFU) + Reset](https://github.com/dbry/lzw-ab). Stuff like this could definetely work better than single nonchanging strategies.
 - Adaptive switching in this case could be done through monitoring compression ratios, like if it gets bad we switch or reset.
 - You could run all strategies and take the lowest compresison ratio? idk
 - This is a stupid project that took up literally a week of my time for nothing, i fucking hate compression LZW can go fuck itself. 🖕🖕🥱🥱🥱🥱🥱
 - Hi wintermute 
 - April Fool's day is Worst than 9/11. I’m fucking shaking and crying right now y’all, and people aren’t taking me seriously. This is a DUMB FUCKING HOLIDAY, where people say shit that ISN’T FUCKING REAL for NO REASON. I’ve cut off 8 family members already for falling for this shriveled up, half-assed ANNUAL CORPORATE FIG LEAF like the NPC SHEEP THEY ARE. Maybe if they listened to REAL COMEDY like Bill Maher or political satire that validates what I already believe in, they’d be WORTHY OF INTERACTING WITH. BUT NO, I have to scroll through my timeline, seething, wailing and gnashing my teeth as I’m BOMBARDED BY LOW EFFORT CORNY CAPITALIST PROPOGANDA. THIS IS A SERIOUS DAY. I’m allowed to be this pressed about ha-ha corny joke day because IT’S SERIOUS FOR ME AND THEREFORE SHOULD BE FOR EVERYONE. My great uncle was tragically flattened while trying to rob a coca-cola vending machine on this date, and PEOPLE ARE STILL MAKING CORNU FUKUNG JOKES. I’ve had enough 
 -  My bf [27M] will only have sex with me [25F] if I wear cat ears and say “Ugh fine, I guess you are my little pogchamp. Come here.”
 - My boyfriend and I have been dating for almost a year now and we’ve been living together for about 2 months. He is very sweet and caring. He has spent plenty of time with me since he got furloughed, but he also spends a considerable amount of time browsing Reddit. I understand that everyone needs space and this is his “alone time”, so I try not to get involved in his “alone time” business. He often likes to quote memes he picks up online. It was dorky in a cute way until about a week ago when he became obsessed with this pogchamp meme.
 - First, he wanted me to call him “my little pogchamp” and rub his belly while we were watching anime. I did because, like I said, it was dorky and cute. The next night, he asked me to call him “pogchamp” in bed. I said it but it was kind of a turnoff because of how oddly obsessed he is with it. 3 nights ago, I wanted to be intimate with him and he asked me to say “Ugh fine, I guess you are my little pogchamp. Come here.” I did but I really didn’t want to. As I was saying it, he popped the biggest boner. Then he had me put on his cat ear gaming headphones while we had sex.
 - The next night he asked me to do the same thing. I was honest and told him it turns me off. He then begged me until his face was bright red and sweaty. I told him to get over it and he literally stormed out of the room and slept on the couch. I tried to convince myself he was just having a bad night, but last night he just stood next to the bed and said “Are you gonna do it?” I told him to stop being silly and he went straight to the couch again!
 - Today while he was at Walmart buying lotion and tissues, I found his laptop open on the couch with a Reddit video of this anime cat girl speaking the exact meme he had been asking me to say. Since I assume he found this meme from Reddit, I am asking Reddit for advice. I am legitimately concerned that my boyfriend is sexually obsessed with an anime cat girl reciting a meme and that it is seriously affecting our physical relationship to the point where he is more attracted to it than me. Am I crazy to think this or are my concerns valid?
