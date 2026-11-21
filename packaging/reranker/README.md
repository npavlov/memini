# CPU reranker graphs

The default Qwen3 reranker uses two packaged graphs on Linux. Both read the original
`model_yesno_quantized.onnx` weights directly from the verified cache.

| Graph | CPUs | Projection method | Uncompressed SHA-256 |
|---|---|---|---|
| `qwen3-per-token-int8.onnx.gz` | arm64, x86-64 with AVX-512 VNNI on all listed CPUs | 197 signed-weight projections with one activation scale per token | `7f8cf431d2c1a36b251357dbff046a2b28480e2ce5bf45b76c81def591f49111` |
| `qwen3-blockwise-int8.onnx.gz` | x86-64 with missing or unconfirmed AVX-512 VNNI | 197 `MatMulNBits` projections with 8-bit weights, blocks of 128, accuracy level 4 | `5eb524d5d293f14cdeae8486bc896a76b7748349ccbf10016fcd499b4dc52b13` |

The blockwise graph repacks the verified weights in memory when ONNX Runtime loads the
session. Both graphs ship inside the wheel. The source model SHA-256 is
`2bc0a77812a15ff6ead003370f36149139ae2c1bb1faf779b00c00886cd919ef`.
[Upstream Apache-2.0 model and tokenizer at the pinned revision](https://huggingface.co/n24q02m/Qwen3-Reranker-0.6B-ONNX/tree/342a8a54de23bba03eb8cce5f5bef503edf18315)

To reproduce both files from the repository root, set `model` to the upstream file path.

```sh
model=/path/to/model_yesno_quantized.onnx
out=$(mktemp -d)
for variant in per-token blockwise; do
    file="qwen3-${variant}-int8.onnx.gz"
    uv run --script packaging/reranker/build.py --variant "$variant" "$model" "$out/$file" &&
        cmp "$out/$file" "src/memini/rerank/$file" || exit 1
done
```

The generator verifies the input and output digests and writes deterministic gzip archives.
Omitting `--variant` builds the per-token graph. The generator dependencies are needed only
when reproducing the graphs.
