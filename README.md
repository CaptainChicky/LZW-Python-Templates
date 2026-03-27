# LZW Compression with Dictionary Management Strategies

A Python implementation of LZW (Lempel-Ziv-Welch) compression exploring how different dictionary-full strategies affect compression ratio, speed, and memory. Includes four base strategies (Freeze, Reset, LRU, LFU), two encoder-decoder synchronization approaches (bitstream signaling vs symmetric/deferred addition), and an optional cascade deletion optimization.

## Table of Contents

- [How LZW Works](#how-lzw-works)
- [The Core Problem: Dictionary Full](#the-core-problem-dictionary-full)
- [Strategies](#strategies)
- [The Encoder-Decoder Sync Problem (LRU/LFU)](#the-encoder-decoder-sync-problem)
- [Complexity Analysis](#complexity-analysis)
- [Cascade Deletion](#cascade-deletion)
- [Benchmark Analysis](#benchmark-analysis)
- [Usage](#usage)
- [Repository Structure](#repository-structure)

## How LZW Works

LZW replaces repeated byte sequences with short codes. The encoder builds a dictionary of seen patterns, outputting a code whenever a match breaks. The decoder reconstructs the same dictionary by watching the pattern of codes. Both sides use variable-width codes (starting at `min_bits`, growing up to `max_bits`) to keep output compact.

Quick example with input `"ababab"` and alphabet `{a:0, b:1}`:

| Step | Phrase | Output | New Entry |
|------|--------|--------|-----------|
| 1-2 | "ab" not found | **0** (a) | 2:"ab" |
| 3 | "ba" not found | **1** (b) | 3:"ba" |
| 4-5 | "ab" found, "aba" not found | **2** (ab) | 4:"aba" |
| 6-EOF | "ab" found, end | **2** (ab) | |

Output: `[0, 1, 2, 2]` -- 4 codes instead of 6 characters.

## The Core Problem: Dictionary Full

Once the dictionary hits `2^max_bits` entries, you can't add more. What you do next determines compression quality on the rest of the file.

## Strategies

### Freeze

Stop adding entries. Compress the rest of the file with the existing dictionary. Simplest possible approach.

**Strengths:** Fastest, lowest memory, excellent on repetitive/uniform data where early patterns cover everything. **Weaknesses:** Cannot adapt after the dictionary fills. Compression degrades on long files with shifting content. Gets *worse* at mid-range `max_bits` (see [Freeze Mid-Range Catastrophe](#the-freeze-mid-range-catastrophe)).

### Reset

When the dictionary fills, send a RESET_CODE signal, clear everything, reinitialize with just the alphabet, and start learning from scratch.

**Strengths:** Adapts to context shifts (archives, multi-section files). Nearly as fast as Freeze. **Weaknesses:** Loses all learned patterns on each reset. Bad on files with globally-common patterns.

### LRU (Least Recently Used)

Evict the least recently used entry and reuse its code slot. Tracks recency with a doubly-linked list + hashmap for O(1) operations.

**Strengths:** Smoothly adapts to shifting contexts without discarding the entire dictionary. Keeps recent patterns alive. **Weaknesses:** More complex, slower than Freeze/Reset, requires solving the encoder-decoder sync problem (see below).

### LFU (Least Frequently Used)

Evict the entry used the fewest times. Uses frequency buckets (hashmap of frequency to doubly-linked list) with LRU tie-breaking within each bucket.

**Strengths:** Preserves globally-common patterns. O(1) eviction via minimum-frequency tracking. **Weaknesses:** Slower than LRU (~2-3x due to higher constant factor per `use()` call: ~20-25 operations vs ~8 for LRU). Has a structural disadvantage in LZW's dictionary building pattern that LRU does not share (see below).

Note: both LRU and LFU share the [orphan problem](#the-orphan-problem) (evicting a parent makes its children unreachable). That affects all eviction strategies equally and is addressed by [cascade deletion](#cascade-deletion). The issues below are specific to LFU's *eviction policy*.

#### Why LFU Underperforms LRU in Practice

LFU has two structural problems that interact badly with LZW:

**Stale entry hoarding.** An entry used 200 times in the first half of a file sits at freq=200, and while not *literally* unevictable, it is effectively stuck for a very long time. You would need to evict 200+ lower-frequency entries before this one becomes the victim. In practice, on most file sizes, high-frequency entries from early in the file occupy slots long after they stop being useful. LRU does not have this problem because unused entries naturally drift to the tail regardless of their history.

**Chain killing.** LZW builds entries incrementally: "ab" (freq=1) gets matched, creating "abc" (freq=1), which gets matched, creating "abcd" (freq=1). Each new entry starts at freq=1 and is immediately among the lowest-frequency entries in the dictionary. In a full dictionary, these new entries are prime eviction candidates before they ever get a chance to be matched. LRU avoids this because new entries start at the MRU position and get a full cycle through the entire list before becoming evictable. This gives them time to actually be matched and extended.

These are not bugs in the implementation. They are inherent properties of LFU as a caching policy, sometimes called "cache pollution." This is well-known in caching systems generally (Redis, for example, uses LFU with time-decay on frequencies to work around it). Vanilla LFU will always have this issue. A potential fix would be adding frequency decay (entries lose frequency over time when not used), but this breaks the O(1) bucket structure and turns it into a different algorithm entirely.

In benchmarks, Symmetric-LRU beats Symmetric-LFU on most files. LFU only wins on data with a truly stable vocabulary from start to finish (e.g., synthetic English-like text with a fixed word list).

## The Encoder-Decoder Sync Problem

LRU and LFU share a fundamental challenge: the encoder and decoder are one step out of sync. The encoder adds an entry at step j, but the decoder can only figure out what that entry is at step j+1. This means when the dictionary fills and eviction starts, the encoder and decoder disagree about which entry occupies which code slot.

This repo implements two independent solutions:

### Approach 1: Bitstream Signaling (EVICT_SIGNAL)

When I first started implementing LRU eviction, bitstream signaling was the only approach I could think of. The encoder tracks evicted codes in a dictionary. When eviction happens, the code slot is reassigned to a new entry and the old-to-new mapping is recorded. Later, if the encoder needs to output that code (now pointing to a different entry than the decoder expects), it first sends an EVICT_SIGNAL in the bitstream telling the decoder "code X now means Y," then outputs the code normally. The decoder has no LRU/LFU tracker of its own; it just follows instructions.

The initial implementation sent the full dictionary entry at *every* eviction. This was a proof-of-concept only; files expanded massively. From there, I optimized in stages:

**Evict-Then-Use Detection.** Not all evictions need a signal. The decoder only breaks if the encoder *outputs* a code whose slot was reassigned. If a code is evicted but never output again, the decoder never notices. This is surprisingly rare (~10-30% of evictions trigger an output of the evicted code). Result: 70-90% reduction in signals.

**Output History + Offset/Suffix.** Compress the signal itself. The evicted entry's prefix is almost always in recent output history. Instead of sending the full string, send a 1-byte offset into a circular buffer of the last 255 outputs plus a 1-byte suffix character. Old format: ~123 bits. New format: ~34 bits. 72% reduction per signal. Fallback to full entry if prefix is not in history (never observed in practice).

**HashMap Lookup.** The output history prefix lookup was originally linear search, $O(255 \times L)$. Adding a hashmap for $O(1)$ prefix lookup gave a 3800x speedup on that operation, for ~4KB of memory overhead.

Only the final optimized version is included in the repository. The earlier versions are trivial to implement from these descriptions.

**Pros:** Fast decompression (decoder is simple). **Cons:** EVICT_SIGNAL adds overhead to the bitstream. Requires output history tracking on the encoder side.

### Approach 2: Symmetric / Deferred Addition

Both encoder and decoder run identical dictionary code. The encoder "defers" its entry addition by one step to match the decoder's natural timing. Both sides add the same entry at the same time, perform the same LRU/LFU operations in the same order, and therefore always agree on evictions.

The key insight is that the entry the encoder would add at step j (`current_match + next_char`) is the same value the decoder computes at step j+1 (`prev_output + current_output[0]`). By making the encoder also use the decoder's formula, both sides stay perfectly synchronized.

**Pros:** Zero bitstream overhead (no EVICT_SIGNAL, no EVICT_SIGNAL code reservation). Simpler encoder (no output history, no evicted_codes tracking). Better compression. **Cons:** Slower decompression (decoder must run its own LRU/LFU tracker). Entries become available one step later (negligible impact in practice).

### Comparison

Across all benchmarks, symmetric beats bitstream on compression ratio in 105 out of 160 tests (LRU) and 99 out of 160 (LFU). Bitstream LRU never wins a single test outright. Bitstream LFU wins exactly 3 tests, all by margins under 2.5 percentage points. The EVICT_SIGNAL overhead is simply too costly.

Symmetric is also ~15% faster to compress than bitstream (no output history management), though ~1.8x slower to decompress (decoder runs its own tracker). If decompression speed is critical, bitstream's dumb-decoder approach has an edge, but the compression ratio gap is hard to justify.

#### Side Note: Deferred Addition + Reset

The deferred/symmetric approach could also be applied to the Reset strategy, eliminating the RESET_CODE signal. Both sides would independently detect "dictionary full" and reset simultaneously. However, Reset's RESET_CODE costs exactly one codeword (trivial overhead), and the sync logic is already dead simple. The complexity savings from eliminating it are near-zero, unlike LRU/LFU where EVICT_SIGNAL is frequent and expensive. Not worth the effort.

## Complexity Analysis

### Why $O(n \cdot L)$ Beats $O(n)$ in Python

All implementations use Python's string-keyed `dict` for the LZW dictionary. The greedy matching loop does:

```python
combined = current + char      # O(L) string concat
if combined in fwd:            # O(L) hash
    current = combined
```

A match of length $L$ costs $O(L^2)$ work (building and hashing strings of length $1, 2, \ldots, L$). Across the whole input, total work is $O(n \cdot L_{avg})$, where average match length grows roughly logarithmically with dictionary size, giving approximately $O(n \log n)$ for typical inputs.

True $O(n)$ (one constant-time operation per input byte) is achievable through several approaches:

- **Trie with generation counters.** Store dictionary as parent-child edges, one edge traversal per byte. Generation counters invalidate stale edges after eviction. Tested in this project but not included in the final repo (see below).
- **Trie with cascade deletion.** Same structure, but recursively evict all descendants when a node is evicted. Amortized $O(n)$.
- **Array-based trie.** Replace hashmap at each node with a fixed 256-pointer array indexed by byte value. No hashing, just array indexing. Memory-hungry but cache-friendly.
- **Double-array trie.** Compact trie using two parallel arrays (base + check). Memory-efficient, cache-friendly, common in production systems. More complex to implement with eviction.

None of these are included in the repository. In pure Python, all $O(n)$ approaches tested were 20-40% *slower* than the string-keyed dict despite better asymptotics. The core issue is that any Python-level $O(1)$ operation (tuple allocation, extra dict lookups, bookkeeping) carries enough interpreter overhead to outweigh the asymptotic gain from avoiding $O(L)$ C-level string hashing. Python's `dict` pushes all work into CPython's optimized C internals, and that constant-factor advantage dominates at practical file sizes.

The crossover where $O(n)$ actually wins would require either much larger files (so $L_{avg}$ grows enough to make string hashing expensive) or a C/Rust implementation (so the constant factors equalize).

Benchmark (trie vs string dict, 200KB repetitive text, max_bits=16):

| Version | Compress | Decompress | Compressed Size |
|---------|----------|------------|-----------------|
| String dict | 0.128s | 0.073s | 44,502 |
| Trie | 0.194s | 0.108s | 44,504 |

Same output, 50% slower. The trie also produces slightly *larger* output at small `max_bits` due to the orphan problem (see below).

### The Orphan Problem

When evicting an entry like "ab" from the dictionary, any entries that extend it ("abc", "abd", etc.) become unreachable. This is true for *both* the string-keyed dict and the trie, because LZW's greedy matching builds strings incrementally. The encoder constructs `combined = "a" + "b" = "ab"` before it can ever try `"abc"`. If `"ab"` is gone, the lookup fails and the encoder never even constructs `"abc"` to look it up, even though it still exists in the dictionary.

These orphaned entries waste dictionary slots until they naturally age out as LRU/LFU victims.

## Cascade Deletion

Cascade deletion fixes the orphan problem: when evicting an entry, also evict all its unreachable descendants, freeing their slots for immediate reuse.

### How It Works

Two extra data structures: `children_of` (maps each entry to the set of its child entries) and `free_codes` (freed code slots available for reuse). When evicting entry X, iteratively evict all children of X, then all children of those children, etc. Use freed slots for new entries before allocating new code numbers.

### Tradeoffs

Amortized O(n) -- each entry is created once and deleted once across the entire run. Individual evictions can be bursty (one eviction deletes 20 nodes), but subsequent additions don't need to evict (free slots available).

Benchmark results (Symmetric LRU, string-keyed dict, 200KB repetitive text):

| max_bits | No Cascade | Cascade | Size Delta | Time Delta |
|----------|-----------|---------|------------|------------|
| 10 | 62,791 | 58,456 | -6.9% | +22% |
| 12 | 55,919 | 52,433 | -6.2% | +27% |
| 16 | 44,502 | 44,502 | 0% | +17% |

On binary-like data (200KB, max_bits=10), the improvement is even larger: -12.1% size for +18% time.

At `max_bits=16`, the dictionary never fills for these file sizes, so cascade does nothing but add overhead: identical output, 15-35% slower.

**Bottom line:** Cascade is worth it when the dictionary is small and under eviction pressure. At typical `max_bits=16` with moderate file sizes, it is pure overhead.

### Generalizability

The included cascade implementation is built on the Symmetric LRU approach (`lzw_cascade_symmetric_lru.py`). The same pattern (add `children_of` tracking, `free_codes` list, and a `cascade_evict` function) applies to any eviction-based strategy: Bitstream LRU, Symmetric LFU, Bitstream LFU. Only one example is included to avoid redundant code.

## Benchmark Analysis

Full results from testing 9 implementations across 20 files at 8 `max_bits` settings (9 through 16):

### Symmetric Crushes Bitstream

Symmetric-LRU beats Bitstream-LRU in 105/160 tests, averaging 84.0% vs 117.0% compression ratio. Bitstream-LRU never wins a single test outright. The EVICT_SIGNAL overhead is simply too large, especially on high-entropy data with frequent evictions.

Symmetric also beats Bitstream on LFU (99 wins vs 47), though the gap is smaller (~8pp average) because bitstream LFU's optimized offset+suffix encoding is more compact than bitstream LRU's.

### Dictionary Size Changes Everything

| max_bits | Freeze wins | Reset wins | Symmetric wins | Bitstream wins |
|----------|-------------|------------|----------------|----------------|
| 9 | 20% | 5% | 65% | 10% |
| 10 | 20% | 45% | 30% | 5% |
| 12 | 30% | 35% | 35% | 0% |
| 14 | 50% | 0% | 50% | 0% |
| 16 | 65% | 5% | 30% | 0% |

At small dictionaries (max_bits 9-11), eviction strategies dominate because the dictionary fills quickly and recycling entries matters. At large dictionaries (max_bits 14-16), Freeze dominates because the dictionary rarely fills and eviction overhead has no benefit.

Reset peaks at max_bits 10-11 (45% wins), occupying a narrow niche where the dictionary fills occasionally and complete resets are cheaper than per-entry eviction.

### File Size Predicts Strategy

| File Size | Dominant Strategy |
|-----------|-------------------|
| Under 10 KB | Freeze (88%) -- dictionary never fills |
| 10-100 KB | Freeze (70%) |
| 100 KB - 1 MB | Three-way tie: Freeze, Reset, Symmetric-LFU (~27% each) |
| Over 1 MB | Symmetric-LFU (38%), Reset (31%), Symmetric-LRU (28%). Freeze wins 0%. |

Freeze never wins on files over 1 MB. On large files the dictionary always fills, and Freeze's inability to adapt is fatal.

### The Freeze Mid-Range Catastrophe

Freeze gets *worse* as you increase `max_bits` from 9 to ~13, then recovers:

| max_bits | bed.jpg (Freeze) |
|----------|------------------|
| 9 | 111.7% |
| 11 | 133.0% |
| 13 | 146.4% (worst) |
| 16 | 125.3% |

This pattern appears on 9 different high-entropy files. At small `max_bits`, the dictionary fills fast and Freeze uses its (small but complete) frozen dictionary. At mid-range `max_bits`, the dictionary takes longer to fill, wasting more bits on wider codewords while learning, but then freezes with still-short entries. At large `max_bits`, the dictionary is big enough to be genuinely useful. The mid-range is the worst of both worlds. If using Freeze, go small or go big.

### Context Shifting: Symmetric-LRU > Reset

On context_shift_200k.bin (4 blocks with distinct byte distributions), Symmetric-LRU beats Reset at every `max_bits` from 9 through 13 (86.1% vs 93.7% at max9, 72.1% vs 74.7% at max13). Reset throws away the whole dictionary at each shift boundary, losing entries that span boundaries. Symmetric-LRU smoothly evicts stale entries one at a time, retaining cross-boundary patterns.

### The PSD Deep-Dive (8.3 MB)

The largest test file showcases strategy differences clearly:

| Strategy | max9 | max12 | max16 |
|----------|------|-------|-------|
| Symmetric-LRU | 60.2% | 58.1% | 57.1% |
| Reset | 65.2% | 58.9% | 58.5% |
| Symmetric-LFU | 85.4% | 78.5% | 73.7% |
| Freeze | 110.3% | 138.4% | 78.5% |
| Bitstream-LRU | 96.3% | 114.3% | 127.1% |

Symmetric-LRU wins at every dictionary size. Bitstream-LRU gets *worse* as dictionary size grows (signal overhead per eviction increases). Freeze is terrible at small dictionaries but catches up at max16.

### Decompression Speed Is Predictable

The decompression speed ranking is the same across every file:

Freeze > Reset > Bitstream-LFU > Bitstream-LRU > Symmetric-LRU > Symmetric-LFU

On the 8.3 MB PSD: Freeze decompresses in 2.7s, Symmetric-LRU in 7.9s, Symmetric-LFU in 9.7s. If decompression speed is critical (read-heavy workloads), bitstream's simple decoder has an advantage despite worse compression.

### Compression Speed

| Strategy | Relative to Freeze |
|----------|-------------------|
| Reset | 1.03x (essentially the same) |
| Symmetric-LRU | 1.65x |
| Symmetric-LFU | 1.76x |
| Bitstream-LFU | 1.89x |
| Bitstream-LRU | 1.92x |

Symmetric is ~15% faster to compress than bitstream (no output history management).

### LRU vs LFU (Symmetric)

Symmetric-LRU beats Symmetric-LFU on most files. LFU only wins on data with a stable vocabulary from start to finish. See [Why LFU Underperforms LRU](#why-lfu-underperforms-lru-in-practice) for the structural explanation.

### When To Use What

| Scenario | Recommendation |
|----------|----------------|
| Repetitive/uniform data | Freeze |
| Archives, multi-section files | Reset or Symmetric-LRU |
| Large text files with stable vocabulary | Symmetric-LFU |
| Large structured binary (PSD, etc.) | Symmetric-LRU |
| Context-shifting data | Symmetric-LRU |
| Small dictionary with eviction pressure | Symmetric-LRU + cascade |
| Decompression speed critical | Freeze or bitstream approach |
| Simplicity | Freeze or Reset |

In practice, Freeze and Reset cover most use cases well. The eviction strategies (LRU/LFU) shine on large files and small dictionaries, but add meaningful complexity.

## Usage

No external libraries needed.

### Compress

```bash
python [strategy].py compress --alphabet [alphabet] --min-bits [num] --max-bits [num] [input] [output]
```

### Decompress

```bash
python [strategy].py decompress [input] [output]
```

### Alphabets

| Name | Size | Description |
|------|------|-------------|
| `ascii` | 128 | Standard ASCII (0-127) |
| `extendedascii` | 256 | Extended ASCII (0-255) |
| `ab` | 2 | Binary alphabet (testing) |

Add custom alphabets in the `ALPHABETS` dict at the top of each file.

### Recommended Parameters

For real files: `--alphabet extendedascii --min-bits 9 --max-bits 16`

For testing eviction behavior: use small `--max-bits` (9-11) to force the dictionary to fill quickly.

## Repository Structure

```
README.md
LICENSE
LZW-Freeze.py
LZW-Reset.py
LZW-Cascade(LRU-Symmetric).py      -- Cascade deletion example

BitstreamEncoding/
    LZW-LRU-Bitstream.py           -- EVICT_SIGNAL approach (hashmap)
    LZW-LFU-Bitstream.py           -- Based on LRU Bitstream

DeferredSymmetric/
    LZW-LRU-Symmetric.py           -- Deferred addition, no signals
    LZW-LFU-Symmetric.py           -- Based on LRU Symmetric
```

### Bitstream Format

```
Header:  [min_bits: 8b] [max_bits: 8b] [alphabet_size: 16b] [alphabet: 8b each]
Body:    Variable-width codewords (min_bits to max_bits)
Footer:  [EOF_CODE at current bit width]
```

Special codes vary by strategy:

| Code | Value | Used By |
|------|-------|---------|
| Alphabet | 0 to N-1 | All |
| EOF_CODE | N | All |
| RESET_CODE | N+1 | Reset only |
| EVICT_SIGNAL | 2^max_bits - 1 | Bitstream LRU/LFU only |

Symmetric strategies use no special codes beyond EOF_CODE. All code slots are available for dictionary entries.

## Possible Extensions

- **Frequency decay LFU:** Instead of raw counts, decay frequency over time to fix the stale-hoarding problem. Turns LFU into a recency-weighted frequency tracker (similar to what Redis does). Breaks the O(1) bucket structure, likely needs a heap for O(log n) eviction.
- **Hybrid strategies:** Adaptive switching between strategies based on monitored compression ratio. If compression degrades, switch or reset.
- **Other eviction policies:** FIFO, MRU, TTL, random replacement. Any cache eviction policy is a potential dictionary management strategy.
- **Cascade deletion for all strategies:** The included cascade example is on Symmetric LRU. The same approach (track `children_of`, `free_codes`, and `cascade_evict`) applies to any eviction-based strategy.