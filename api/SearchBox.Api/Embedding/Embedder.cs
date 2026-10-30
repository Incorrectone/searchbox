using System.Security.Cryptography;
using Microsoft.ML.OnnxRuntime;
using Microsoft.ML.OnnxRuntime.Tensors;
using Microsoft.ML.Tokenizers;

namespace SearchBox.Api.Embedding;

/// <summary>
/// Turns a search query into the same 384-number vector the crawler stores for
/// each post, using the identical model.onnx (checked by SHA-256 at startup).
///
/// Steps, mirroring crawler/antiseo_crawler/embedder.py:
///   1. WordPiece tokenization (lower-cased, accents stripped, [CLS] ... [SEP], max 256 tokens)
///   2. Run the ONNX model: one 384-d vector per token
///   3. Mean-pool over real tokens, then L2-normalize to length 1
/// </summary>
public sealed class Embedder : IDisposable
{
    public const int Dimensions = 384;
    private const int MaxTokens = 256;

    private readonly InferenceSession _session;
    private readonly BertTokenizer _tokenizer;
    private readonly bool _needsTokenTypeIds;

    /// <summary>Hex SHA-256 of model.onnx: must equal meta.embedding_model written by the crawler.</summary>
    public string ModelSha256 { get; }

    public Embedder(string modelDir)
    {
        var modelPath = Path.Combine(modelDir, "model.onnx");
        var vocabPath = Path.Combine(modelDir, "vocab.txt");
        if (!File.Exists(modelPath) || !File.Exists(vocabPath))
            throw new FileNotFoundException($"model.onnx and vocab.txt are required in {modelDir}");

        using (var stream = File.OpenRead(modelPath))
            ModelSha256 = Convert.ToHexStringLower(SHA256.HashData(stream));

        var options = new Microsoft.ML.OnnxRuntime.SessionOptions
        {
            IntraOpNumThreads = 2, // the app VM has 2 vCPUs
            InterOpNumThreads = 1,
            GraphOptimizationLevel = GraphOptimizationLevel.ORT_ENABLE_ALL,
        };
        _session = new InferenceSession(modelPath, options);
        _needsTokenTypeIds = _session.InputMetadata.ContainsKey("token_type_ids");

        _tokenizer = BertTokenizer.Create(vocabPath, new BertOptions
        {
            LowerCaseBeforeTokenization = true, // all-MiniLM-L6-v2 is an uncased model
            RemoveNonSpacingMarks = true,       // "café" -> "cafe", like the Python tokenizer
        });
    }

    /// <summary>Embeds one text. Thread-safe: ONNX Runtime sessions allow concurrent Run calls.</summary>
    public float[] Embed(string text)
    {
        IReadOnlyList<int> ids = _tokenizer.EncodeToIds(
            text, MaxTokens, addSpecialTokens: true, out _, out _,
            considerPreTokenization: true, considerNormalization: true);

        int n = ids.Count;
        var inputIds = new DenseTensor<long>([1, n]);
        var attention = new DenseTensor<long>([1, n]);
        var tokenTypes = new DenseTensor<long>([1, n]); // all zeros: single sentence
        for (int i = 0; i < n; i++)
        {
            inputIds[0, i] = ids[i];
            attention[0, i] = 1;
        }

        var inputs = new List<NamedOnnxValue>
        {
            NamedOnnxValue.CreateFromTensor("input_ids", inputIds),
            NamedOnnxValue.CreateFromTensor("attention_mask", attention),
        };
        if (_needsTokenTypeIds)
            inputs.Add(NamedOnnxValue.CreateFromTensor("token_type_ids", tokenTypes));

        using var results = _session.Run(inputs);
        var tokenVectors = results[0].AsTensor<float>(); // shape [1, n, 384]

        // Mean pooling. No padding here (batch of one), so every token counts.
        var vec = new float[Dimensions];
        for (int t = 0; t < n; t++)
            for (int d = 0; d < Dimensions; d++)
                vec[d] += tokenVectors[0, t, d];

        double norm = 0;
        for (int d = 0; d < Dimensions; d++)
        {
            vec[d] /= n;
            norm += vec[d] * vec[d];
        }
        norm = Math.Max(Math.Sqrt(norm), 1e-12);
        for (int d = 0; d < Dimensions; d++)
            vec[d] = (float)(vec[d] / norm);
        return vec;
    }

    /// <summary>pgvector's text format, e.g. "[0.012345,-0.2,...]", cast in SQL with ::halfvec(384).</summary>
    public static string ToPgVector(float[] v) =>
        "[" + string.Join(',', v.Select(x => x.ToString("F6", System.Globalization.CultureInfo.InvariantCulture))) + "]";

    public void Dispose() => _session.Dispose();
}
