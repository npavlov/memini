# CPU reranker graphs

The default Qwen3 reranker uses two packaged graphs on Linux. Both use one activation scale per token and read the verified upstream weights.

| Graph | Linux CPUs | Weight representation | Uncompressed SHA-256 |
|---|---|---|---|
| `qwen3-per-token-int8.onnx.gz` | arm64 or x86-64 with AVX-512 VNNI on every listed CPU | Original INT8 | `7f8cf431d2c1a36b251357dbff046a2b28480e2ce5bf45b76c81def591f49111` |
| `qwen3-channel7-int8.onnx.gz` | x86-64 with missing or unconfirmed AVX-512 VNNI | Symmetric seven-bit weights per output channel | `64a83156c2b65a655d523d2fe54001da822325298ad42b5197ee736f41d0e849` |

On legacy x86, ONNX Runtime uses `MatMulInteger` for the 197 weight projections. Each weight lies in [−63, 63], so two products with unsigned eight-bit activations have magnitude at most 32,130 and fit into the signed 16-bit intermediate. The graph stores each output channel’s weight scale. At startup, NumPy reads one matrix at a time from the verified cache, computes its Q7 weights with nearest-even rounding, and supplies them through `SessionOptions.add_external_initializers`. The cache can be mounted read-only.

The source model SHA-256 is `2bc0a77812a15ff6ead003370f36149139ae2c1bb1faf779b00c00886cd919ef`. The initializer names, byte offsets and dimensions are pinned in `qwen3-channel7.json` with SHA-256 `725464f6c5ff41460c60dad4cf192d9d3cddb0b773355c3c48f284be6d35bdc4`. [Upstream Apache-2.0 model and tokenizer](https://huggingface.co/n24q02m/Qwen3-Reranker-0.6B-ONNX/tree/342a8a54de23bba03eb8cce5f5bef503edf18315)

To regenerate the graphs and metadata from the repository root:

```sh
model=/path/to/model_yesno_quantized.onnx
out=$(mktemp -d)
uv run --script packaging/reranker/build.py "$model" "$out/qwen3-per-token-int8.onnx.gz"
uv run --script packaging/reranker/build.py --variant channel7 \
    --metadata "$out/qwen3-channel7.json" "$model" "$out/qwen3-channel7-int8.onnx.gz"
for name in qwen3-per-token-int8.onnx.gz qwen3-channel7-int8.onnx.gz qwen3-channel7.json; do
    cmp "$out/$name" "src/memini/rerank/$name" || exit 1
done
```
