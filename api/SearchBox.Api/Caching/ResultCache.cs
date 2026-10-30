using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SearchBox.Api.Redis;
using StackExchange.Redis;

namespace SearchBox.Api.Caching;

/// <summary>
/// Redis cache for search results and AI summaries, with "admit on second hit".
///
/// Most queries are typed once and never again ("one-hit wonders"). Caching
/// those wastes Redis writes and quota. So the first time a normalized query is
/// seen, we only remember it in an in-memory Bloom filter and don't touch Redis
/// at all. From the second time on, we read Redis and store the result on a miss.
///
/// With one API server, "not in the filter" reliably means "not in Redis", so
/// random-query bots cost the cache nothing.
///
/// Fail-open: any Redis error is treated as a cache miss.
/// </summary>
public sealed class ResultCache(RedisStore redis, ILogger<ResultCache> log)
{
    private const int FilterCapacity = 100_000;
    private BloomFilter _seen = new(FilterCapacity, 0.01);
    private DateOnly _filterDay = DateOnly.FromDateTime(DateTime.UtcNow);
    private readonly object _rotateLock = new();

    /// <summary>"  Rust   MEMORY leak " -> "rust memory leak"</summary>
    public static string Normalize(string query) =>
        string.Join(' ', query.Trim().ToLowerInvariant()
            .Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));

    public static bool IsCacheable(string normalized) =>
        normalized.Length is > 0 and <= 200;

    /// <summary>Cache key, e.g. search:v12:3f5a... (index version invalidates old entries).</summary>
    public static string Key(string kind, long indexVersion, string normalizedParts)
    {
        var hash = Convert.ToHexStringLower(SHA1.HashData(Encoding.UTF8.GetBytes(normalizedParts)));
        return $"{kind}:v{indexVersion}:{hash}";
    }

    /// <summary>
    /// Returns the cached value, or computes it. `cached` tells the caller
    /// whether the result came from Redis.
    /// </summary>
    public async Task<(T Value, bool Cached)> GetOrComputeAsync<T>(
        string kind, long indexVersion, string normalizedParts, TimeSpan ttl,
        Func<Task<T>> compute, Func<T, bool>? shouldStore = null)
    {
        if (!IsCacheable(normalizedParts))
            return (await compute(), false);

        RotateFilterDaily();
        bool seenBefore = _seen.CheckAndAdd($"{kind}|{normalizedParts}");
        var db = redis.Db;
        if (!seenBefore || db is null)
            return (await compute(), false); // first sighting: zero Redis commands

        string key = Key(kind, indexVersion, normalizedParts);
        try
        {
            RedisValue hit = await db.StringGetAsync(key);
            if (hit.HasValue)
            {
                var value = JsonSerializer.Deserialize<T>((string)hit!, JsonOptions);
                if (value is not null) return (value, true);
            }
        }
        catch (Exception ex) when (ex is RedisException or TimeoutException or JsonException)
        {
            log.LogDebug(ex, "Cache read failed; computing");
        }

        T fresh = await compute();
        if (shouldStore is null || shouldStore(fresh))
        {
            try
            {
                await db.StringSetAsync(key, JsonSerializer.Serialize(fresh, JsonOptions), ttl);
            }
            catch (Exception ex) when (ex is RedisException or TimeoutException)
            {
                log.LogDebug(ex, "Cache write failed; ignoring");
            }
        }
        return (fresh, false);
    }

    /// <summary>A fresh filter every day, or it slowly fills up and says "seen" for everything.</summary>
    private void RotateFilterDaily()
    {
        var today = DateOnly.FromDateTime(DateTime.UtcNow);
        if (today == _filterDay && _seen.Count < FilterCapacity) return;
        lock (_rotateLock)
        {
            if (today == _filterDay && _seen.Count < FilterCapacity) return;
            _seen = new BloomFilter(FilterCapacity, 0.01);
            _filterDay = today;
        }
    }

    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);
}
