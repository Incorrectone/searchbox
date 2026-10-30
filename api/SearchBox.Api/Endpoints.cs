using System.Diagnostics;
using SearchBox.Api.Caching;
using SearchBox.Api.Data;
using SearchBox.Api.Embedding;
using SearchBox.Api.Limits;
using SearchBox.Api.Redis;
using SearchBox.Api.Summary;

namespace SearchBox.Api;

// ---- Response shapes (serialized as snake_case JSON, e.g. took_ms) ----

public sealed record ResultDto(
    string Title, string Url, string Host, DateTimeOffset? PublishedAt, string Snippet,
    MatchDto Match, float? QualityScore, ThumbDto? Thumb);

public sealed record MatchDto(int? SemanticRank, int? KeywordRank, double Score);

public sealed record ThumbDto(string Url, int Width, int Height, string? Alt);

public sealed record SearchResponse(
    string Query, int Page, int PageSize, long TotalCandidates, long TookMs, bool Cached,
    long IndexVersion, IReadOnlyList<ResultDto> Results);

public sealed record SummaryResponse(
    string Query, string Answer, IReadOnlyList<Citation> Sources, string Model, long TookMs, bool Cached);

/// <summary>
/// The HTTP API. ASP.NET "minimal APIs": each MapGet line connects a URL to a
/// function. Parameters are filled in automatically: query-string values by
/// name, and services (Embedder, SearchRepository, ...) from dependency injection.
/// </summary>
public static class Endpoints
{
    // Leaky-bucket policies (capacity, drain per second). See Limits/LeakyBucket.cs.
    private static readonly BucketPolicy SearchPolicy = new("search", 10, 1 / 3.0);   // burst 10, then 20/min
    private static readonly BucketPolicy ApiPolicy = new("api", 30, 1);                 // burst 30, then 60/min
    private static readonly BucketPolicy SummaryPolicy = new("summary", 3, 1 / 20.0);   // burst 3, then 3/min

    public static void MapSearchBoxApi(this WebApplication app)
    {
        var api = app.MapGroup("/api/v1");

        api.MapGet("/search", SearchAsync);
        api.MapGet("/images", ImagesAsync);
        api.MapGet("/summary", SummaryAsync);
        api.MapGet("/stats", StatsAsync);
        api.MapGet("/health", HealthAsync);
    }

    private static string ClientIp(HttpContext ctx) =>
        ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown"; // already the real IP: see ForwardedHeaders in Program.cs

    /// <summary>Returns a 429 response if the caller's bucket is full, otherwise null.</summary>
    private static async Task<IResult?> LimitAsync(LeakyBucket buckets, BucketPolicy policy, HttpContext ctx)
    {
        var r = await buckets.TryTakeAsync(policy, ClientIp(ctx));
        if (r.Allowed) return null;
        ctx.Response.Headers.RetryAfter = r.RetryAfterSeconds.ToString();
        return Results.Json(new { error = "rate_limited", retry_after = r.RetryAfterSeconds }, statusCode: 429);
    }

    private static IResult? Validate(string? q, int page, int pageSize, int maxPageSize)
    {
        if (string.IsNullOrWhiteSpace(q) || q.Length > 300)
            return Results.BadRequest(new { error = "q must be 1-300 characters" });
        if (page is < 1 or > 10)
            return Results.BadRequest(new { error = "page must be 1-10" });
        if (pageSize < 1 || pageSize > maxPageSize)
            return Results.BadRequest(new { error = $"page_size must be 1-{maxPageSize}" });
        return null;
    }

    private static ResultDto ToDto(SearchHit h, SearchBoxOptions o) => new(
        h.Title, h.Url, h.Host, h.PublishedAt, h.Snippet,
        new MatchDto(h.SemanticRank, h.KeywordRank, Math.Round(h.Score, 5)),
        h.QualityScore,
        h.ThumbPath is null ? null : new ThumbDto(o.ThumbBaseUrl + h.ThumbPath, h.ThumbWidth ?? 0, h.ThumbHeight ?? 0, h.ThumbAlt));

    // GET /api/v1/search?q=rust+memory+leak&page=1&page_size=10
    private static async Task<IResult> SearchAsync(
        string? q, int? page, int? page_size, HttpContext ctx,
        LeakyBucket buckets, ResultCache cache, IndexInfo index,
        Embedder embedder, SearchRepository repo, SearchBoxOptions options, CancellationToken ct)
    {
        int p = page ?? 1, size = page_size ?? 10;
        if (Validate(q, p, size, 50) is { } bad) return bad;
        if (await LimitAsync(buckets, SearchPolicy, ctx) is { } limited) return limited;

        var sw = Stopwatch.StartNew();
        string norm = ResultCache.Normalize(q!);
        var (body, cached) = await cache.GetOrComputeAsync(
            "search", index.IndexVersion, $"{norm}|{p}|{size}", TimeSpan.FromHours(1),
            async () =>
            {
                var vec = embedder.Embed(q!);
                var result = await repo.SearchAsync(vec, q!, size, (p - 1) * size, ct);
                return new SearchResponse(q!, p, size, result.TotalCandidates, 0, false, index.IndexVersion,
                    result.Hits.Select(h => ToDto(h, options)).ToList());
            });
        return Results.Ok(body with { Query = q!, TookMs = sw.ElapsedMilliseconds, Cached = cached });
    }

    // GET /api/v1/images?q=...&page=1  (posts that have a thumbnail)
    private static async Task<IResult> ImagesAsync(
        string? q, int? page, HttpContext ctx,
        LeakyBucket buckets, ResultCache cache, IndexInfo index,
        Embedder embedder, SearchRepository repo, SearchBoxOptions options, CancellationToken ct)
    {
        int p = page ?? 1, size = 24;
        if (Validate(q, p, size, 24) is { } bad) return bad;
        if (await LimitAsync(buckets, SearchPolicy, ctx) is { } limited) return limited;

        var sw = Stopwatch.StartNew();
        string norm = ResultCache.Normalize(q!);
        var (body, cached) = await cache.GetOrComputeAsync(
            "images", index.IndexVersion, $"{norm}|{p}", TimeSpan.FromHours(1),
            async () =>
            {
                var vec = embedder.Embed(q!);
                var result = await repo.SearchImagesAsync(vec, q!, size, (p - 1) * size, ct);
                return new SearchResponse(q!, p, size, result.TotalCandidates, 0, false, index.IndexVersion,
                    result.Hits.Select(h => ToDto(h, options)).ToList());
            });
        return Results.Ok(body with { Query = q!, TookMs = sw.ElapsedMilliseconds, Cached = cached });
    }

    // GET /api/v1/summary?q=...   (only called when the user clicks "Summarize")
    private static async Task<IResult> SummaryAsync(
        string? q, HttpContext ctx,
        LeakyBucket buckets, ResultCache cache, IndexInfo index, Embedder embedder,
        SearchRepository repo, SummaryService summaries, CancellationToken ct)
    {
        if (!summaries.Enabled) return Results.Json(new { error = "summary_disabled" }, statusCode: 503);
        if (Validate(q, 1, 5, 5) is { } bad) return bad;
        if (await LimitAsync(buckets, SummaryPolicy, ctx) is { } limited) return limited;

        var sw = Stopwatch.StartNew();
        string norm = ResultCache.Normalize(q!);
        var (result, cached) = await cache.GetOrComputeAsync<SummaryResult?>(
            "summary", index.IndexVersion, norm, TimeSpan.FromHours(24),
            async () =>
            {
                var vec = embedder.Embed(q!);
                var top = await repo.SearchAsync(vec, q!, 5, 0, ct);
                var sources = await repo.GetSourcesAsync(top.Hits.Select(h => h.Id).ToList(), 900, ct);
                return await summaries.SummarizeAsync(q!, sources, ct);
            },
            shouldStore: r => r is not null); // never cache "no model available"

        if (result is null)
            return Results.Json(new { error = "summary_unavailable" }, statusCode: 503);
        return Results.Ok(new SummaryResponse(q!, result.Answer, result.Sources, result.Model, sw.ElapsedMilliseconds, cached));
    }

    // GET /api/v1/stats  (index size and last crawl, shown in the UI)
    private static async Task<IResult> StatsAsync(
        HttpContext ctx, LeakyBucket buckets, IndexInfo index, SummaryService summaries,
        Embedder embedder, SearchBoxOptions options, CancellationToken ct)
    {
        if (await LimitAsync(buckets, ApiPolicy, ctx) is { } limited) return limited;
        if (index.Current is null) await index.RefreshAsync(ct);
        var s = index.Current;
        return Results.Ok(new
        {
            doc_count = s?.DocCount ?? 0,
            domain_count = s?.DomainCount ?? 0,
            image_count = s?.ImageCount ?? 0,
            last_crawl_at = s?.LastCrawlAt,
            index_version = s?.IndexVersion ?? 0,
            embedding_model = embedder.ModelSha256[..12],
            summary_enabled = summaries.Enabled,
            summary_models = summaries.ModelNames,
            dashboard_url = string.IsNullOrWhiteSpace(options.DashboardUrl) ? null : options.DashboardUrl,
        });
    }

    // GET /api/v1/health  (UptimeRobot). 503 only if Postgres is down.
    private static async Task<IResult> HealthAsync(SearchRepository repo, RedisStore redis, Embedder embedder, CancellationToken ct)
    {
        string pg, rd;
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(TimeSpan.FromSeconds(3));
            await repo.PingAsync(cts.Token);
            pg = "ok";
        }
        catch (Exception) { pg = "down"; }

        if (!redis.Configured) rd = "disabled";
        else
        {
            try { rd = redis.Db is { } db && (await db.PingAsync()).TotalMilliseconds >= 0 ? "ok" : "down"; }
            catch (Exception) { rd = "down"; } // the PING also keeps the free Upstash database active
        }

        var body = new { status = pg == "ok" ? "ok" : "degraded", postgres = pg, redis = rd, model = embedder.ModelSha256[..12] };
        return pg == "ok" ? Results.Ok(body) : Results.Json(body, statusCode: 503);
    }
}
