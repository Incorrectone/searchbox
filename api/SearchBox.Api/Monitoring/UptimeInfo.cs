using System.Globalization;
using System.Text.Json;

namespace SearchBox.Api.Monitoring;

/// <summary>Uptime figures for the UI: current state plus 7- and 30-day ratios (percent).</summary>
public sealed record UptimeStats(string Status, double? Ratio7d, double? Ratio30d, DateTimeOffset CheckedAt);

/// <summary>
/// Asks UptimeRobot for this site's monitor every 5 minutes and keeps the
/// latest answer in memory, like IndexInfo does for the index stats.
///
/// The key is a MONITOR-SPECIFIC key (it can only read that one monitor) and
/// never leaves the server: browsers get the numbers through /api/v1/stats, so
/// the CSP stays "connect-src 'self'". 12 calls an hour is far below the free
/// plan's 10 requests a minute. No key configured = feature off, nothing shown.
/// </summary>
public sealed class UptimeInfo(IHttpClientFactory httpFactory, SearchBoxOptions options, ILogger<UptimeInfo> log)
    : BackgroundService
{
    public const string HttpClientName = "uptimerobot";
    private static readonly TimeSpan Interval = TimeSpan.FromMinutes(5);
    private static readonly TimeSpan MaxAge = TimeSpan.FromHours(1); // older than this = don't show

    private volatile UptimeStats? _stats;

    public bool Enabled => !string.IsNullOrWhiteSpace(options.UptimeRobotKey);

    /// <summary>The latest figures, or null if disabled, not fetched yet, or stale.</summary>
    public UptimeStats? Current =>
        _stats is { } s && DateTimeOffset.UtcNow - s.CheckedAt < MaxAge ? s : null;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (!Enabled) return;
        using var timer = new PeriodicTimer(Interval);
        do
        {
            await RefreshAsync(stoppingToken);
        } while (await timer.WaitForNextTickAsync(stoppingToken));
    }

    public async Task RefreshAsync(CancellationToken ct)
    {
        try
        {
            var http = httpFactory.CreateClient(HttpClientName);
            using var form = new FormUrlEncodedContent(new Dictionary<string, string>
            {
                ["api_key"] = options.UptimeRobotKey,
                ["format"] = "json",
                ["custom_uptime_ratios"] = "7-30",
            });
            using var response = await http.PostAsync("https://api.uptimerobot.com/v2/getMonitors", form, ct);
            var body = await response.Content.ReadAsStringAsync(ct);
            if (!response.IsSuccessStatusCode)
            {
                log.LogWarning("UptimeRobot returned {Status}", (int)response.StatusCode);
                return;
            }
            var parsed = Parse(body, DateTimeOffset.UtcNow);
            if (parsed is null)
            {
                // Their error message (e.g. "api_key not found"), never the key itself.
                log.LogWarning("UptimeRobot answer not usable: {Body}", body.Length > 200 ? body[..200] : body);
                return;
            }
            _stats = parsed;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException)
        {
            if (!ct.IsCancellationRequested) log.LogWarning(ex, "UptimeRobot request failed");
        }
    }

    /// <summary>
    /// Reads a v2 getMonitors answer: {"stat":"ok","monitors":[{"status":2,
    /// "custom_uptime_ratio":"99.987-100.000", ...}]}. Uses the first monitor,
    /// which is the only one a monitor-specific key can see.
    /// </summary>
    public static UptimeStats? Parse(string json, DateTimeOffset now)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        if (!root.TryGetProperty("stat", out var stat) || stat.GetString() != "ok") return null;
        if (!root.TryGetProperty("monitors", out var monitors) || monitors.ValueKind != JsonValueKind.Array
            || monitors.GetArrayLength() == 0) return null;

        var m = monitors[0];
        int code = m.TryGetProperty("status", out var st) && st.TryGetInt32(out var c) ? c : -1;
        string status = code switch
        {
            2 => "up",
            8 or 9 => "down",
            0 => "paused",
            1 => "not_checked",
            _ => "unknown",
        };

        double? r7 = null, r30 = null;
        if (m.TryGetProperty("custom_uptime_ratio", out var ratios) && ratios.ValueKind == JsonValueKind.String)
        {
            var parts = ratios.GetString()!.Split('-');
            r7 = ParsePercent(parts.ElementAtOrDefault(0));
            r30 = ParsePercent(parts.ElementAtOrDefault(1));
        }
        return new UptimeStats(status, r7, r30, now);
    }

    private static double? ParsePercent(string? s) =>
        double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var v) && v is >= 0 and <= 100
            ? Math.Round(v, 3)
            : null;
}
