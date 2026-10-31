# How the SearchBox API works: a C# / .NET guide

This explains the API code for someone who knows programming (Python, say) but not C# or .NET. Read it alongside the code: it follows a request from the moment it arrives.

---

## 1. .NET in five minutes

| Concept | What it means here | Python analogy |
|---|---|---|
| **.NET** | The runtime that executes C# programs (memory management, threads, standard library). | The Python interpreter |
| **C#** | The language. Statically typed: every variable has a type the compiler checks *before* the program runs. | Python with mandatory type hints that are actually enforced |
| **SDK vs runtime** | The SDK (`dotnet` command) builds code; the runtime only runs it. | `pip` + build tools vs plain `python` |
| **`.csproj` file** | The project file: target framework (`net10.0`) and dependencies. | `pyproject.toml` / `requirements.txt` |
| **NuGet** | The package registry (`dotnet add package Npgsql`). | PyPI |
| **`dotnet build`** | Compiles C# into *IL* (intermediate code) in a `.dll`. Type errors stop the build here. | (Python has no compile step) |
| **`dotnet run` / `dotnet X.dll`** | Runs it. The runtime JIT-compiles IL to machine code as it goes. | `python app.py` |
| **`dotnet publish -r linux-arm64 --self-contained`** | Bundles the app *and* the runtime for one OS/CPU. The app VM needs no .NET installed. | Like PyInstaller |
| **`dotnet test`** | Runs the xUnit tests in `SearchBox.Api.Tests/`. | `pytest` |

**ASP.NET Core** is .NET's web framework, and **Kestrel** is its built-in web server, so no nginx or gunicorn is needed in front of it.

---

## 2. C# syntax you'll meet in this code

```csharp
var x = 5;                         // type inferred (int). Still statically typed.
string? title = null;              // "?" = may be null. Without it the compiler warns on null.
int n = page ?? 1;                 // "??" = use 1 if page is null  (Python: page or 1, roughly)
r.IsDBNull(4) ? null : r.GetString(4)   // ternary: cond ? a : b  (Python: a if cond else b)
$"search:v{version}:{hash}"        // string interpolation (Python f-string)
"""                                // raw string literal: multi-line, no escaping (the SQL uses these)
  SELECT ...
  """
List<long> ids = [1, 2, 3];        // collection expression (Python list literal)
x => x * 2                         // lambda (Python: lambda x: x * 2)
hits.Select(h => h.Id).ToList()    // LINQ: map/filter on collections (Python: [h.id for h in hits])
```

**Records.** Immutable data classes with value equality, made in one line:
```csharp
public sealed record Citation(int N, string Title, string Url);
var c2 = c1 with { Title = "new" };   // copy with one field changed (used for TookMs/Cached)
```
Like a frozen Python `@dataclass`.

**Primary constructors.** `public sealed class SearchRepository(NpgsqlDataSource db)` declares a constructor parameter `db` that the whole class can use. It's a shorthand for a private field set in a constructor.

**async / await.** Same idea as Python's asyncio. `async Task<T>` is like `async def ... -> T`. `await` pauses *this* request while waiting on the database or network, and the thread serves other requests meanwhile. That's why one small VM can handle many concurrent searches.

**using / await using.** Guarantees cleanup, like Python's `with`:
```csharp
await using var conn = await db.OpenConnectionAsync(ct);   // connection returned to the pool at end of scope
```

**CancellationToken `ct`.** Passed through every async call. If the visitor closes the tab, ASP.NET cancels the token, and the database query stops instead of wasting work.

**Exceptions with filters.** `catch (Exception ex) when (ex is RedisException or TimeoutException)` catches only those types. This is how the code fails open on Redis problems without hiding real bugs.

**`sealed`, `static`, `partial`.**
- `sealed`: the class can't be subclassed.
- `static`: belongs to the class, not an instance.
- `partial`: the class is split across files. `[GeneratedRegex]` uses this: the compiler writes the regex code at build time.

---

## 3. The shape of an ASP.NET Core app (`Program.cs`)

`Program.cs` runs top to bottom once at startup:

1. **Configuration.** `appsettings.json` is read first, then **environment variables override it**. A double underscore means nesting: `SearchBox__Gemini__ApiKey=...` sets `SearchBox:Gemini:ApiKey`. On the VM those come from `/etc/searchbox/api.env` (loaded by systemd), and the values are bound into the `SearchBoxOptions` class (`Options.cs`).
2. **Dependency injection (DI).** `builder.Services.AddSingleton<SearchRepository>()` registers a class. When anything needs a `SearchRepository`, in a constructor *or as an endpoint parameter*, ASP.NET creates one and passes it in. You never write `new SearchRepository(...)` yourself, and tests can swap in fakes. `Singleton` means one shared instance for the app's lifetime, which is right for connection pools and the ONNX model (loaded once, about 30 MB).
3. **Startup check.** Before serving anything, it compares the SHA-256 of `model.onnx` with `meta.embedding_model` written by the crawler. If they differ, it refuses to start: queries embedded with a different model would silently return garbage.
4. **Middleware pipeline.** `app.UseXxx()` steps run in order for every request, like layers of an onion:
   - `UseForwardedHeaders`: replaces the connection's IP with the real client IP from `X-Forwarded-For`, but only if the request came from the edge VM (`10.20.1.4`). Anyone else's header is ignored, so nobody can fake their IP to dodge rate limits.
   - A small custom step adds the Content-Security-Policy and `nosniff` headers.
   - `UseDefaultFiles` + `UseStaticFiles` serve the UI from `wwwroot/`.
5. **Endpoints.** `app.MapSearchBoxApi()` (in `Endpoints.cs`) connects URLs to functions.
6. **`app.Run()`** starts Kestrel on port 8080 and blocks.

There is also an `embed-dump` mode at the top, used only for the Python↔C# embedding parity check.

---

## 4. Following one search request

`GET /api/v1/search?q=rust+memory+leak` → `Endpoints.SearchAsync`:

```csharp
private static async Task<IResult> SearchAsync(
    string? q, int? page, int? page_size, HttpContext ctx,       // from the query string / request
    LeakyBucket buckets, ResultCache cache, IndexInfo index,    // from dependency injection
    Embedder embedder, SearchRepository repo, SearchBoxOptions options, CancellationToken ct)
```
ASP.NET fills these parameters automatically. Simple types come from the query string by name, and service types come from DI. The steps:

1. **Validate** `q` (1–300 chars), `page` (1–10), `page_size` (1–50). Bad input gets HTTP 400.
2. **Rate limit**: `LeakyBucket.TryTakeAsync` (see §5). If the bucket is full, the response is HTTP 429 with `Retry-After`.
3. **Cache** (`ResultCache.GetOrComputeAsync`, see §6). Returns a cached result or runs the lambda:
4. **Embed** the query: `Embedder.Embed` (see §7). Turns the text into 384 numbers.
5. **Query Postgres**: `SearchRepository.SearchAsync` runs the hybrid SQL (see §8).
6. **Respond.** The record is serialized to JSON with snake_case names (`took_ms`, `page_size`, …), as configured in `Program.cs`.

---

## 5. The leaky-bucket rate limiter (`Limits/LeakyBucket.cs`)

Each `(policy, IP)` pair has a bucket. A request adds one unit; the bucket drains at a fixed rate; overflow means 429.

| Policy | Capacity (burst) | Drain | Sustained |
|---|---|---|---|
| search / images | 10 | 1 every 3 s | 20/min |
| summary | 3 | 1 every 20 s | 3/min |
| stats | 30 | 1/s | 60/min |
| Gemini model (global, not per IP) | its per-minute limit | per-minute limit / 60 | just under Google's quota |

**Why Redis, and why Lua.** The state (`level`, `last` timestamp) lives in a Redis hash, so it survives restarts and would be shared across several API servers. "Read level → compute drain → write new level" must be **atomic**: otherwise two simultaneous requests both read "9 of 10" and both get through. Redis runs a Lua script as one indivisible step, so the whole algorithm is a 15-line script sent with `ScriptEvaluateAsync`. It uses Redis's own clock (`TIME`), so app servers with drifting clocks can't disagree. `EXPIRE` deletes buckets of IPs that went quiet.

**Fail open.** If Redis is down or slow (over 400 ms), the exception is caught and the same algorithm runs in a `ConcurrentDictionary` in memory. Limits still apply; they just aren't shared.

---

## 6. The result cache (`Caching/`)

- **`ResultCache.Normalize`**: `"  Rust MEMORY  leak"` → `"rust memory leak"`, so trivial variations share one cache entry.
- **Key**: `search:v{index_version}:{sha1(normalized|page|size)}`. `IndexInfo` (a `BackgroundService` that re-reads the `meta` table every 60 s) supplies `index_version`. When a crawl finishes and bumps it, every old key stops matching. Nothing has to be deleted.
- **Admission on second hit (`BloomFilter.cs`).** Most queries are one-offs. The first time a query is seen, it's only recorded in an in-memory Bloom filter and **Redis isn't touched at all**. From the second time, Redis is read; on a miss, the result is computed and stored for 1 hour. The Bloom filter is a bit array plus several hash functions: about 120 KB remembers 100,000 queries with a 1% false-positive rate. It resets daily so it never fills up.
- **Fail open.** Any Redis error counts as a cache miss.

---

## 7. Embeddings in C# (`Embedding/Embedder.cs`)

This mirrors `crawler/antiseo_crawler/embedder.py` exactly. Both must turn the same text into the same vector, or search breaks.

1. **Tokenize** with `Microsoft.ML.Tokenizers.BertTokenizer`, reading `vocab.txt`:
   - lowercase and strip accents (the model is "uncased");
   - split into word pieces (`"leaks"` → `"leak", "##s"`);
   - add `[CLS]` … `[SEP]`;
   - cut to 256 tokens.
2. **Run the model** with `Microsoft.ML.OnnxRuntime`. Inputs are three tensors of shape `[1, n]`: token IDs, the attention mask (all 1s), and token type IDs (all 0s). The output is `[1, n, 384]`, one vector per token.
3. **Mean-pool** the n token vectors into one, then **L2-normalize** it to length 1, so cosine similarity is just a dot product.
4. **`ToPgVector`** formats it as `[0.012,-0.2,...]` text, which SQL casts with `::halfvec(384)`.

**Parity check.** During development, the same five texts were embedded in Python and in C#, and every pair had cosine similarity 1.000000. That test found a real bug: Python wasn't adding `[CLS]`/`[SEP]` when the tokenizer file lacked that rule. Run it yourself on the real model:
```bash
dotnet SearchBox.Api.dll embed-dump /path/to/minilm texts.txt > cs.jsonl
```
and compare against the crawler's embeddings of the same lines.

---

## 8. The SQL (`Data/SearchRepository.cs`)

- **`NpgsqlDataSource`** is the connection pool. `OpenConnectionAsync` borrows a connection; `await using` returns it.
- **Parameters** (`@q`, `@v`, `@limit`) are sent separately from the SQL text, so user input can never change the query's structure. That's what prevents SQL injection. The `api` role can only `SELECT` anyway.
- **One transaction per search**, so `SET LOCAL` applies only to that search:
  - `hnsw.ef_search = 100`: without it, the vector index returns at most 40 candidates even though the query asks for 100;
  - `statement_timeout = 5s`: a runaway query can't hang a request.
- **The query, CTE by CTE:**

| CTE | What it does |
|---|---|
| `sem` | The 100 posts whose embedding is closest to the query (`<=>` is cosine distance, served by the HNSW index), numbered 1–100 |
| `kw` | The 100 best full-text matches (`websearch_to_tsquery` understands quotes and `-word`), numbered 1–100 |
| `fused` | **Reciprocal Rank Fusion**: score = Σ 1/(60 + rank) over the lists a post is in. Keeps each post's rank in each list for the UI's "S/K" badges |
| `page` | Sorts, then applies `LIMIT/OFFSET`; `count(*) OVER ()` gives the total before the limit |
| final `SELECT` | Joins title, URL, domain and thumbnail. `ts_headline` builds the snippet with matched words wrapped in invisible marker characters (`\u0002`/`\u0003`), which the UI turns into `<b>` safely |

`ts_headline` runs only on the 10 rows of the current page, which is why the `page` CTE exists.

---

## 9. The AI summary (`Summary/`)

`GET /api/v1/summary?q=` (only when the visitor clicks *Summarize*):
1. Per-IP leaky bucket (3 burst, 3/min).
2. Cache with second-hit admission, 24 h TTL. A failed summary is never cached.
3. Search, take the top 5 posts, and load the first 900 characters of each (`GetSourcesAsync`).
4. `SummaryService.SummarizeAsync` walks the model list (Flash Lite, then Gemma 4). For each model it checks:
   - its **global** per-minute bucket (shared by all visitors, because Google's quota is per project);
   - its **daily** counter (`DailyQuota`: Redis `INCR`, reset at midnight Pacific like Google's);
   - then calls `POST .../models/{id}:generateContent`. A 429 or an error moves on to the next model.
5. If no model works, the API returns 503 and the UI hides the box. Search is unaffected.

**Prompt-injection defences.**
- Sources go in a `<sources>` block, and the instructions say they're data.
- Source **URLs are never sent** to the model.
- The answer is cleaned to plain text (`Clean`: control characters and markdown removed).
- The UI renders it with `textContent` and turns only `[n]` into links, pointing to the URLs *we* returned.
- A hijacked answer can say anything, but it can't create a clickable link or inject HTML.

The whole prompt goes in the user turn because Gemma models reject system instructions.

---

## 10. The UI (`wwwroot/`)

Plain HTML, CSS and JavaScript, served by the API itself (`UseStaticFiles`); there's no build step. `app.js` reads `?q=&tab=&page=` from the URL, calls the API, and builds the page with `document.createElement` + `textContent`, **never `innerHTML`**. The Content-Security-Policy header blocks inline scripts, so even a missed escape couldn't run code.

---

## 11. Tests (`SearchBox.Api.Tests/CoreTests.cs`)

xUnit. `[Fact]` is a test; `[Theory]` with `[InlineData(...)]` runs the same test with several inputs. They cover:
- the Bloom filter: no false negatives, false-positive rate, sizing;
- query normalization and cache keys;
- Gemini response parsing (skipping "thought" parts) and answer cleaning;
- the prompt structure (sources delimited, URLs never included);
- Redis URL parsing (TLS, credentials).

Run them with `dotnet test` from `api/`.

The integration behaviour (rate limits, caching, fallback, Redis outage) was tested against real Postgres and Redis during development. The steps are in `README.md` under "Verify".

---

## 12. Reading order and likely interview questions

**Suggested reading order:** `Program.cs` → `Endpoints.cs` (SearchAsync) → `Limits/LeakyBucket.cs` → `Caching/ResultCache.cs` → `Data/SearchRepository.cs` → `Embedding/Embedder.cs` → `Summary/SummaryService.cs`.

| Question | Short answer |
|---|---|
| Why a leaky bucket, not a fixed window? | A fixed window allows 2× the limit across a window boundary; a bucket smooths bursts and caps the sustained rate. |
| Why is the Lua script needed? | Atomic read-modify-write; without it, concurrent requests race past the limit. |
| What if Redis dies? | Everything fails open: the same limiter runs in memory, and the cache becomes a pass-through. Search never depends on Redis. |
| Why cache on the second hit? | Most queries are one-offs. Caching them wastes writes, quota and memory. The Bloom filter makes "first sighting" free. |
| How do you know Python and C# embeddings match? | The model file is pinned by hash and checked at startup, plus a parity test (cosine 1.000000 on test texts). |
| How is the client IP trusted? | `X-Forwarded-For` is accepted only from the edge VM's fixed private IP; the edge sets it from Cloudflare's `CF-Connecting-IP`, trusted only from Cloudflare's ranges. |
| How do you stop prompt injection? | Delimited sources, no URLs in the prompt, plain-text output, citation-only links to our own URLs, no tools. |
| Why `SET LOCAL hnsw.ef_search`? | HNSW returns at most `ef_search` (default 40) candidates; the query wants 100. |
