using System.Collections.Concurrent;
using SearchBox.Api.Redis;
using StackExchange.Redis;

namespace SearchBox.Api.Summary;

/// <summary>
/// Counts requests per model per day, so we stop just before AI Studio's
/// requests-per-day quota (which resets at midnight Pacific time).
/// Redis INCR is atomic; if Redis is unavailable we count in memory.
/// </summary>
public sealed class DailyQuota(RedisStore redis, ILogger<DailyQuota> log)
{
    private readonly ConcurrentDictionary<string, int> _memory = new();

    private static string PacificDay()
    {
        try
        {
            var tz = TimeZoneInfo.FindSystemTimeZoneById("America/Los_Angeles");
            return TimeZoneInfo.ConvertTimeFromUtc(DateTime.UtcNow, tz).ToString("yyyyMMdd");
        }
        catch (TimeZoneNotFoundException)
        {
            return DateTime.UtcNow.ToString("yyyyMMdd");
        }
    }

    /// <summary>Takes one unit of today's quota; false if the limit is reached.</summary>
    public async Task<bool> TryTakeAsync(string model, int limit)
    {
        string key = $"quota:{model}:{PacificDay()}";
        var db = redis.Db;
        if (db is not null)
        {
            try
            {
                long n = await db.StringIncrementAsync(key);
                if (n == 1) await db.KeyExpireAsync(key, TimeSpan.FromHours(26));
                return n <= limit;
            }
            catch (Exception ex) when (ex is RedisException or TimeoutException)
            {
                log.LogDebug(ex, "Redis quota counter failed; counting in memory");
            }
        }
        int count = _memory.AddOrUpdate(key, 1, (_, c) => c + 1);
        return count <= limit;
    }
}
