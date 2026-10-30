using System.Collections.Concurrent;
using SearchBox.Api.Redis;
using StackExchange.Redis;

namespace SearchBox.Api.Limits;

/// <summary>A bucket: holds up to Capacity units and drains RatePerSecond units every second.</summary>
public sealed record BucketPolicy(string Name, double Capacity, double RatePerSecond);

public readonly record struct LimitResult(bool Allowed, int RetryAfterSeconds, bool FromRedis);

/// <summary>
/// Leaky-bucket rate limiter.
///
/// Each key (for example "search:203.0.113.7") has a bucket. Every request pours
/// in one unit; the bucket drains at a fixed rate; a request that would overflow
/// it is rejected with HTTP 429 and a Retry-After header. Short bursts up to
/// Capacity are fine, but the long-run rate can never exceed the drain rate.
///
/// The bucket state lives in Redis so it survives API restarts and would be
/// shared by several API servers. The read-modify-write happens inside one Lua
/// script, which Redis runs atomically: two simultaneous requests can't both
/// read "9 of 10" and both get in.
///
/// If Redis is missing, slow or failing, the same algorithm runs in memory
/// ("fail open"): limits still apply, they just aren't shared.
/// </summary>
public sealed class LeakyBucket(RedisStore redis, ILogger<LeakyBucket> log)
{
    private const string Script = """
        local level = tonumber(redis.call('HGET', KEYS[1], 'level')) or 0
        local last  = tonumber(redis.call('HGET', KEYS[1], 'last'))  or 0
        local t     = redis.call('TIME')
        local now   = tonumber(t[1]) + tonumber(t[2]) / 1000000
        local rate  = tonumber(ARGV[1])
        local cap   = tonumber(ARGV[2])
        level = math.max(0, level - (now - last) * rate)
        if level + 1 > cap then
          return {0, math.ceil((level + 1 - cap) / rate)}
        end
        level = level + 1
        redis.call('HSET', KEYS[1], 'level', tostring(level), 'last', tostring(now))
        redis.call('EXPIRE', KEYS[1], math.ceil(level / rate) + 1)
        return {1, 0}
        """;

    private readonly ConcurrentDictionary<string, MemoryBucket> _memory = new();
    private DateTime _lastSweep = DateTime.UtcNow;

    public async Task<LimitResult> TryTakeAsync(BucketPolicy policy, string subject)
    {
        string key = $"rl:{policy.Name}:{subject}";
        var db = redis.Db;
        if (db is not null)
        {
            try
            {
                // Redis returns numbers, so cap/rate are passed as strings and parsed in Lua.
                var result = (RedisResult[])(await db.ScriptEvaluateAsync(
                    Script, [key],
                    [policy.RatePerSecond.ToString(System.Globalization.CultureInfo.InvariantCulture),
                     policy.Capacity.ToString(System.Globalization.CultureInfo.InvariantCulture)]))!;
                return new LimitResult((int)result[0] == 1, (int)result[1], FromRedis: true);
            }
            catch (Exception ex) when (ex is RedisException or TimeoutException)
            {
                log.LogDebug(ex, "Redis rate limit failed; using in-memory bucket");
            }
        }
        return TakeFromMemory(key, policy);
    }

    private LimitResult TakeFromMemory(string key, BucketPolicy policy)
    {
        SweepOccasionally();
        var bucket = _memory.GetOrAdd(key, _ => new MemoryBucket());
        lock (bucket)
        {
            double now = Environment.TickCount64 / 1000.0;
            bucket.Level = Math.Max(0, bucket.Level - (now - bucket.Last) * policy.RatePerSecond);
            bucket.Last = now;
            if (bucket.Level + 1 > policy.Capacity)
                return new LimitResult(false, (int)Math.Ceiling((bucket.Level + 1 - policy.Capacity) / policy.RatePerSecond), false);
            bucket.Level += 1;
            return new LimitResult(true, 0, false);
        }
    }

    /// <summary>Drop idle in-memory buckets every few minutes so memory can't grow forever.</summary>
    private void SweepOccasionally()
    {
        if (DateTime.UtcNow - _lastSweep < TimeSpan.FromMinutes(5)) return;
        _lastSweep = DateTime.UtcNow;
        double cutoff = Environment.TickCount64 / 1000.0 - 600;
        foreach (var (k, b) in _memory)
            if (b.Last < cutoff) _memory.TryRemove(k, out _);
    }

    private sealed class MemoryBucket
    {
        public double Level;
        public double Last = Environment.TickCount64 / 1000.0;
    }
}
