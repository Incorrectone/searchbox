using StackExchange.Redis;

namespace SearchBox.Api.Redis;

/// <summary>
/// One shared connection to Upstash (or null when no RedisUrl is configured).
///
/// Everything that uses Redis must "fail open": every call has a short timeout,
/// and callers catch exceptions and fall back to in-memory behaviour. A dead or
/// over-quota Redis must never make search fail.
/// </summary>
public sealed class RedisStore : IDisposable
{
    private readonly ConnectionMultiplexer? _mux;
    private readonly ILogger<RedisStore> _log;

    public RedisStore(SearchBoxOptions options, ILogger<RedisStore> log)
    {
        _log = log;
        if (string.IsNullOrWhiteSpace(options.RedisUrl))
        {
            log.LogWarning("No RedisUrl configured: using in-memory rate limits and no shared cache");
            return;
        }
        _mux = ConnectionMultiplexer.Connect(ParseUrl(options.RedisUrl));
    }

    /// <summary>Null when Redis isn't configured or isn't connected right now.</summary>
    public IDatabase? Db => _mux is { IsConnected: true } ? _mux.GetDatabase() : null;

    public bool Configured => _mux is not null;

    /// <summary>
    /// rediss://user:password@host:port  ->  StackExchange.Redis options.
    /// "rediss" (two s) means TLS, which Upstash requires.
    /// </summary>
    public static ConfigurationOptions ParseUrl(string url)
    {
        var uri = new Uri(url);
        var options = new ConfigurationOptions
        {
            Ssl = uri.Scheme == "rediss",
            AbortOnConnectFail = false, // start even if Redis is down; reconnect in the background
            ConnectTimeout = 3000,
            SyncTimeout = 400,
            AsyncTimeout = 400,         // Singapore is ~35 ms away; anything slower = treat as down
            ConnectRetry = 2,
        };
        options.EndPoints.Add(uri.Host, uri.Port > 0 ? uri.Port : 6379);
        if (!string.IsNullOrEmpty(uri.UserInfo))
        {
            var parts = uri.UserInfo.Split(':', 2);
            if (parts.Length == 2)
            {
                options.User = Uri.UnescapeDataString(parts[0]);
                options.Password = Uri.UnescapeDataString(parts[1]);
            }
            else
            {
                options.Password = Uri.UnescapeDataString(parts[0]);
            }
        }
        return options;
    }

    public void Dispose() => _mux?.Dispose();
}
