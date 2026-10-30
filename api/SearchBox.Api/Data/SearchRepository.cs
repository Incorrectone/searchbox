using Npgsql;
using SearchBox.Api.Embedding;

namespace SearchBox.Api.Data;

public sealed record SearchHit(
    long Id,
    string Title,
    string Url,
    string Host,
    DateTimeOffset? PublishedAt,
    string Snippet,          // contains \u0002 ... \u0003 around matched words (the UI bolds them)
    int? SemanticRank,       // position in the meaning-based ranking (null = not in top 100)
    int? KeywordRank,        // position in the keyword ranking (null = no keyword match)
    double Score,            // Reciprocal Rank Fusion score
    float? QualityScore,
    string? ThumbPath,       // blob path, e.g. "ab/ab12...ef.webp", or null
    int? ThumbWidth,
    int? ThumbHeight,
    string? ThumbAlt);

public sealed record SearchPage(long TotalCandidates, IReadOnlyList<SearchHit> Hits);

public sealed record IndexStats(
    long DocCount, long DomainCount, long ImageCount,
    string? LastCrawlAt, long IndexVersion, string? EmbeddingModel);

/// <summary>
/// All SQL lives here. Uses the read-only `api` database role, so even a bug
/// (or an injection, which parameterized queries already prevent) cannot write.
/// </summary>
public sealed class SearchRepository(NpgsqlDataSource db)
{
    // Markers that ts_headline puts around matched words. Control characters
    // can't appear in normal text, so the UI can safely turn them into <b>.
    public const char HighlightStart = '\u0002';
    public const char HighlightEnd = '\u0003';
    private static readonly string HeadlineOptions =
        $"MaxWords=40, MinWords=20, MaxFragments=2, FragmentDelimiter=\" … \", StartSel={HighlightStart}, StopSel={HighlightEnd}";

    /// <summary>
    /// Hybrid search: the 100 nearest posts by meaning (HNSW on embeddings) and
    /// the 100 best keyword matches (full-text search), fused with Reciprocal
    /// Rank Fusion: score = sum of 1 / (60 + rank) over the lists a post appears in.
    /// </summary>
    private static string BuildSql(bool imagesOnly)
    {
        string join = imagesOnly ? "JOIN images im ON im.document_id = d.id" : "";
        return $"""
            WITH sem AS (
              SELECT id, row_number() OVER (ORDER BY dist) AS r FROM (
                SELECT d.id, d.embedding <=> @v::halfvec(384) AS dist
                FROM documents d {join}
                ORDER BY dist LIMIT 100) s),
            kw AS (
              SELECT id, row_number() OVER (ORDER BY rank DESC) AS r FROM (
                SELECT d.id, ts_rank_cd(d.tsv, websearch_to_tsquery('english', @q)) AS rank
                FROM documents d {join}
                WHERE d.tsv @@ websearch_to_tsquery('english', @q)
                ORDER BY rank DESC LIMIT 100) k),
            fused AS (
              SELECT id,
                     SUM(1.0 / (60 + r)) AS score,
                     MIN(r) FILTER (WHERE src = 's') AS sem_r,
                     MIN(r) FILTER (WHERE src = 'k') AS kw_r
              FROM (SELECT id, r, 's' AS src FROM sem UNION ALL SELECT id, r, 'k' FROM kw) u
              GROUP BY id),
            page AS (
              SELECT f.*, count(*) OVER () AS total
              FROM fused f
              ORDER BY f.score DESC, f.id
              LIMIT @limit OFFSET @offset)
            SELECT d.id, d.title, d.url, dm.host, d.published_at,
                   ts_headline('english', left(coalesce(d.body, ''), 20000),
                               websearch_to_tsquery('english', @q), @hl) AS snippet,
                   p.sem_r, p.kw_r, p.score, d.quality_score,
                   i.blob_path, i.width, i.height, i.alt, p.total
            FROM page p
            JOIN documents d ON d.id = p.id
            JOIN domains dm ON dm.id = d.domain_id
            LEFT JOIN images i ON i.document_id = d.id
            ORDER BY p.score DESC, p.id
            """;
    }

    private static readonly string SearchSql = BuildSql(imagesOnly: false);
    private static readonly string ImageSql = BuildSql(imagesOnly: true);

    public Task<SearchPage> SearchAsync(float[] queryVector, string query, int limit, int offset, CancellationToken ct) =>
        RunAsync(SearchSql, queryVector, query, limit, offset, ct);

    public Task<SearchPage> SearchImagesAsync(float[] queryVector, string query, int limit, int offset, CancellationToken ct) =>
        RunAsync(ImageSql, queryVector, query, limit, offset, ct);

    private async Task<SearchPage> RunAsync(string sql, float[] vec, string query, int limit, int offset, CancellationToken ct)
    {
        await using var conn = await db.OpenConnectionAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);

        // SET LOCAL only lasts until the end of this transaction.
        // ef_search must be >= the inner LIMIT 100, or HNSW returns at most 40 candidates.
        await using (var set = new NpgsqlCommand(
            "SET LOCAL hnsw.ef_search = 100; SET LOCAL statement_timeout = '5s'", conn, tx))
            await set.ExecuteNonQueryAsync(ct);

        await using var cmd = new NpgsqlCommand(sql, conn, tx);
        cmd.Parameters.AddWithValue("v", Embedder.ToPgVector(vec));
        cmd.Parameters.AddWithValue("q", query);
        cmd.Parameters.AddWithValue("hl", HeadlineOptions);
        cmd.Parameters.AddWithValue("limit", limit);
        cmd.Parameters.AddWithValue("offset", offset);

        var hits = new List<SearchHit>();
        long total = 0;
        await using (var r = await cmd.ExecuteReaderAsync(ct))
        {
            while (await r.ReadAsync(ct))
            {
                hits.Add(new SearchHit(
                    Id: r.GetInt64(0),
                    Title: r.GetString(1),
                    Url: r.GetString(2),
                    Host: r.GetString(3),
                    PublishedAt: r.IsDBNull(4) ? null : r.GetFieldValue<DateTimeOffset>(4),
                    Snippet: r.IsDBNull(5) ? "" : r.GetString(5),
                    SemanticRank: r.IsDBNull(6) ? null : (int)r.GetInt64(6),
                    KeywordRank: r.IsDBNull(7) ? null : (int)r.GetInt64(7),
                    Score: (double)r.GetDecimal(8),
                    QualityScore: r.IsDBNull(9) ? null : r.GetFloat(9),
                    ThumbPath: r.IsDBNull(10) ? null : r.GetString(10),
                    ThumbWidth: r.IsDBNull(11) ? null : r.GetInt32(11),
                    ThumbHeight: r.IsDBNull(12) ? null : r.GetInt32(12),
                    ThumbAlt: r.IsDBNull(13) ? null : r.GetString(13)));
                total = r.GetInt64(14);
            }
        }
        await tx.CommitAsync(ct);
        return new SearchPage(total, hits);
    }

    public async Task<IndexStats> GetStatsAsync(CancellationToken ct)
    {
        const string sql = """
            SELECT (SELECT count(*) FROM documents),
                   (SELECT count(*) FROM domains WHERE status = 'active'),
                   (SELECT count(*) FROM images),
                   (SELECT value FROM meta WHERE key = 'last_crawl_at'),
                   (SELECT value FROM meta WHERE key = 'index_version'),
                   (SELECT value FROM meta WHERE key = 'embedding_model')
            """;
        await using var cmd = db.CreateCommand(sql);
        await using var r = await cmd.ExecuteReaderAsync(ct);
        await r.ReadAsync(ct);
        return new IndexStats(
            r.GetInt64(0), r.GetInt64(1), r.GetInt64(2),
            r.IsDBNull(3) ? null : r.GetString(3),
            r.IsDBNull(4) ? 0 : long.Parse(r.GetString(4)),
            r.IsDBNull(5) ? null : r.GetString(5));
    }

    /// <summary>Title, URL and the opening of each post, in the given order (for the AI summary).</summary>
    public async Task<IReadOnlyList<(string Title, string Url, string Text)>> GetSourcesAsync(
        IReadOnlyList<long> ids, int maxChars, CancellationToken ct)
    {
        await using var cmd = db.CreateCommand(
            "SELECT id, title, url, left(coalesce(body, summary, ''), @n) FROM documents WHERE id = ANY(@ids)");
        cmd.Parameters.AddWithValue("ids", ids.ToArray());
        cmd.Parameters.AddWithValue("n", maxChars);
        var byId = new Dictionary<long, (string, string, string)>();
        await using (var r = await cmd.ExecuteReaderAsync(ct))
            while (await r.ReadAsync(ct))
                byId[r.GetInt64(0)] = (r.GetString(1), r.GetString(2), r.GetString(3));
        return ids.Where(byId.ContainsKey).Select(id => byId[id]).ToList();
    }

    public async Task PingAsync(CancellationToken ct)
    {
        await using var cmd = db.CreateCommand("SELECT 1");
        await cmd.ExecuteScalarAsync(ct);
    }
}
