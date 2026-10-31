namespace SearchBox.Api;

/// <summary>
/// All settings, bound from the "SearchBox" section of configuration.
/// In production they come from environment variables in /etc/searchbox/api.env,
/// where a double underscore means "nested": SearchBox__Postgres=... sets Postgres.
/// </summary>
public sealed class SearchBoxOptions
{
    /// <summary>Npgsql connection string for the read-only `api` role.</summary>
    public string Postgres { get; set; } = "";

    /// <summary>Upstash URL, e.g. rediss://default:PASSWORD@xxx.upstash.io:6379. Empty = run without Redis.</summary>
    public string RedisUrl { get; set; } = "";

    /// <summary>Folder containing model.onnx and vocab.txt (copied from the Pi/Codespace).</summary>
    public string ModelDir { get; set; } = "/opt/searchbox/models/minilm";

    /// <summary>The only address allowed to set X-Forwarded-For: the edge VM.</summary>
    public string TrustedProxy { get; set; } = "10.20.1.4";

    /// <summary>Public path prefix where thumbnails are served (Caddy proxies /img/* to Blob storage).</summary>
    public string ThumbBaseUrl { get; set; } = "/img/";

    /// <summary>Optional public Looker Studio embed URL for the security dashboard gadget.</summary>
    public string DashboardUrl { get; set; } = "";

    public GeminiOptions Gemini { get; set; } = new();
}

public sealed class GeminiOptions
{
    /// <summary>
    /// Sent as x-goog-api-key. With the relay (relay/), this is the relay token and the
    /// real Gemini key lives only on the relay. Empty = the summary feature is switched off.
    /// </summary>
    public string ApiKey { get; set; } = "";

    public string Endpoint { get; set; } = "https://generativelanguage.googleapis.com/v1beta";

    /// <summary>
    /// Tried in order. Set in appsettings.json (not here: .NET appends configured
    /// list items to code defaults instead of replacing them). Copy the exact model
    /// IDs from AI Studio; Google renames them often. Each limit is kept a little
    /// under the AI Studio quota so we switch models before Google rejects requests.
    /// </summary>
    public List<GeminiModel> Models { get; set; } = []; // defaults live in appsettings.json
}

public sealed class GeminiModel
{
    public string Id { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public int PerMinute { get; set; }
    public int PerDay { get; set; }
}
