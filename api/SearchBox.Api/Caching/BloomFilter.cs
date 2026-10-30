using System.Collections;
using System.IO.Hashing;
using System.Text;

namespace SearchBox.Api.Caching;

/// <summary>
/// A Bloom filter: a compact, approximate set.
///
/// Asking "have I seen this?" gives either "definitely not" or "probably yes".
/// It never says "no" for something that was added, but it sometimes says
/// "yes" for something that wasn't (a false positive). In exchange it is tiny:
/// 100,000 entries at a 1% false-positive rate need about 120 KB.
///
/// How it works: a bit array of m bits, and k hash functions. Adding an item sets
/// the k bits its hashes point at; checking an item tests whether all k are set.
/// The k hashes come from one 128-bit hash split into two 64-bit halves h1, h2,
/// combined as h1 + i*h2 ("double hashing"), which is as good as k independent hashes.
/// </summary>
public sealed class BloomFilter
{
    private readonly BitArray _bits;
    private readonly int _m;
    private readonly int _k;
    private readonly object _lock = new();

    public int Count { get; private set; }
    public int Capacity { get; }

    public BloomFilter(int capacity, double falsePositiveRate)
    {
        Capacity = capacity;
        // Standard sizing formulas: m = -n ln p / (ln 2)^2,  k = (m / n) ln 2
        _m = (int)Math.Ceiling(-capacity * Math.Log(falsePositiveRate) / (Math.Log(2) * Math.Log(2)));
        _k = Math.Max(1, (int)Math.Round((double)_m / capacity * Math.Log(2)));
        _bits = new BitArray(_m);
    }

    public int SizeInBytes => (_m + 7) / 8;

    /// <summary>Adds the item and returns whether it was (probably) already present.</summary>
    public bool CheckAndAdd(string item)
    {
        var (h1, h2) = Hash(item);
        lock (_lock)
        {
            bool present = true;
            for (int i = 0; i < _k; i++)
            {
                int bit = Index(h1, h2, i);
                if (!_bits[bit]) { present = false; _bits[bit] = true; }
            }
            if (!present) Count++;
            return present;
        }
    }

    public bool MightContain(string item)
    {
        var (h1, h2) = Hash(item);
        lock (_lock)
        {
            for (int i = 0; i < _k; i++)
                if (!_bits[Index(h1, h2, i)]) return false;
            return true;
        }
    }

    private int Index(ulong h1, ulong h2, int i) => (int)((h1 + (ulong)i * h2) % (ulong)_m);

    private static (ulong, ulong) Hash(string item)
    {
        var hash = XxHash128.HashToUInt128(Encoding.UTF8.GetBytes(item));
        return ((ulong)(hash >> 64), (ulong)hash | 1); // h2 odd, so steps never repeat early
    }
}
