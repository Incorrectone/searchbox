// Entry point. In .NET, this file's top-level code runs when the app starts.
//
// Structure of an ASP.NET Core app:
//   1. builder  = WebApplication.CreateBuilder(...)  -> configuration + "services"
//   2. builder.Services.AddSingleton<T>(...)          -> register objects for dependency injection
//   3. app = builder.Build()                          -> the configured app
//   4. app.UseXxx(...)                                -> middleware: steps every request passes through, in order
//   5. app.MapGet(...)                                -> endpoints: URL -> function
//   6. app.Run()                                      -> start the web server (Kestrel) and block
using System.Net;
using System.Text.Json;
using Microsoft.AspNetCore.HttpOverrides;
using Npgsql;
using SearchBox.Api;
using SearchBox.Api.Caching;
using SearchBox.Api.Data;
using SearchBox.Api.Embedding;
using SearchBox.Api.Limits;
using SearchBox.Api.Redis;
using SearchBox.Api.Summary;

// Utility mode for the embedding parity check (see README):
//   dotnet SearchBox.Api.dll embed-dump <model-dir> <texts.txt>   -> one JSON vector per line
if (args.Length == 3 && args[0] == "embed-dump")
{
    using var e = new Embedder(args[1]);
    foreach (var line in File.ReadLines(args[2]).Where(l => l.Length > 0))
        Console.WriteLine(JsonSerializer.Serialize(e.Embed(line)));
    return;
}

var builder = WebApplication.CreateBuilder(args);

// ---- Configuration: appsettings.json, then environment variables (SearchBox__X=...) ----
var options = builder.Configuration.GetSection("SearchBox").Get<SearchBoxOptions>() ?? new SearchBoxOptions();
if (string.IsNullOrWhiteSpace(options.Postgres))
    throw new InvalidOperationException("SearchBox__Postgres is not set");

// ---- Services (dependency injection) ----
// AddSingleton = one shared instance for the whole app. Any class that asks for
// one of these in its constructor (or endpoint parameters) receives it automatically.
builder.Services.AddSingleton(options);
builder.Services.AddSingleton(NpgsqlDataSource.Create(options.Postgres)); // connection pool
builder.Services.AddSingleton<SearchRepository>();
builder.Services.AddSingleton(_ => new Embedder(options.ModelDir));         // loads the ONNX model once
builder.Services.AddSingleton<RedisStore>();
builder.Services.AddSingleton<LeakyBucket>();
builder.Services.AddSingleton<ResultCache>();
builder.Services.AddSingleton<DailyQuota>();
builder.Services.AddSingleton<IndexInfo>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<IndexInfo>()); // start its refresh loop
builder.Services.AddHttpClient<SummaryService>(c => c.Timeout = TimeSpan.FromSeconds(25));

// JSON: snake_case field names (took_ms, page_size, ...) like the plan's API spec.
builder.Services.ConfigureHttpJsonOptions(o =>
{
    o.SerializerOptions.PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower;
    o.SerializerOptions.DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.Never;
});

// Real client IP: trust X-Forwarded-For ONLY when the request comes from the edge VM.
// Anyone else sending that header is ignored, so nobody can spoof their IP to dodge rate limits.
builder.Services.Configure<ForwardedHeadersOptions>(o =>
{
    o.ForwardedHeaders = ForwardedHeaders.XForwardedFor;
    o.ForwardLimit = 1;
    o.KnownProxies.Clear();
    o.KnownIPNetworks.Clear();
    o.KnownProxies.Add(IPAddress.Parse(options.TrustedProxy));
});

builder.Services.AddOpenApi(); // serves /openapi/v1.json

var app = builder.Build();

// ---- Startup check: the API must embed queries with the same model the crawler used ----
{
    var embedder = app.Services.GetRequiredService<Embedder>();
    var index = app.Services.GetRequiredService<IndexInfo>();
    await index.RefreshAsync(CancellationToken.None);
    var stored = index.Current?.EmbeddingModel;
    if (stored is null)
        app.Logger.LogWarning("meta.embedding_model is empty (no crawl yet?). Model {Hash}", embedder.ModelSha256[..12]);
    else if (stored != embedder.ModelSha256)
        throw new InvalidOperationException(
            $"Model mismatch: the index was built with {stored[..12]}..., this API has {embedder.ModelSha256[..12]}... " +
            "Copy the crawler's exact model.onnx to ModelDir.");
}

// ---- Middleware pipeline (runs top to bottom for every request) ----
app.UseForwardedHeaders();

app.Use(async (ctx, next) =>
{
    // Content-Security-Policy for the UI: only our own scripts/styles may run,
    // which blocks injected <script> even if some text slipped through unescaped.
    ctx.Response.Headers.ContentSecurityPolicy =
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; " +
        "connect-src 'self'; frame-src https://lookerstudio.google.com; base-uri 'none'; " +
        "form-action 'self'; frame-ancestors 'none'";
    ctx.Response.Headers.XContentTypeOptions = "nosniff";
    await next();
});

app.UseDefaultFiles(); // "/" -> wwwroot/index.html
app.UseStaticFiles();  // serves wwwroot/ (the retro UI)

app.MapOpenApi();
app.MapSearchBoxApi();

app.Run();

// Lets the test project start the app in memory (WebApplicationFactory<Program>).
public partial class Program;
