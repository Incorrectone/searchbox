namespace SearchBox.Api.Data;

/// <summary>
/// Keeps a copy of the index stats (doc count, index_version, ...) refreshed every
/// 60 seconds. index_version is part of every cache key, so when the crawler
/// finishes and bumps it, all old cached results stop matching automatically.
///
/// This is a BackgroundService: ASP.NET starts it with the app and runs
/// ExecuteAsync on a background thread until shutdown.
/// </summary>
public sealed class IndexInfo(SearchRepository repo, ILogger<IndexInfo> log) : BackgroundService
{
    private volatile IndexStats? _stats;

    public IndexStats? Current => _stats;
    public long IndexVersion => _stats?.IndexVersion ?? 0;

    public async Task RefreshAsync(CancellationToken ct)
    {
        try
        {
            _stats = await repo.GetStatsAsync(ct);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            log.LogWarning(ex, "Could not refresh index stats");
        }
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(60));
        do
        {
            await RefreshAsync(stoppingToken);
        } while (await timer.WaitForNextTickAsync(stoppingToken));
    }
}
