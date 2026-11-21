# CPU reranker graph

`src/memini/rerank/qwen3-blockwise-int8.onnx.gz` is a companion execution graph for the
default Qwen3 reranker. Its 197 projections use ONNX Runtime's `MatMulNBits` with 8-bit
weights, 128-element blocks and accuracy level 4. The graph reorders the original weight
bytes and repeats their existing scale across blocks. Large tensors refer to offsets in
`model_yesno_quantized.onnx`, so the wheel carries 60,832 bytes of compressed graph data.
The original model and tokenizer retain their existing verification and download paths.

To reproduce the graph from the pinned model, run from the repository root:

```sh
uv run --script packaging/reranker/build.py /path/to/model_yesno_quantized.onnx /tmp/qwen3-blockwise-int8.onnx.gz
cmp /tmp/qwen3-blockwise-int8.onnx.gz src/memini/rerank/qwen3-blockwise-int8.onnx.gz
```

The generator verifies the upstream SHA-256 before converting the projections and checks
the resulting uncompressed graph's SHA-256. The gzip header has a fixed timestamp and
contains no local filename. The generator's ONNX tooling is needed only for regeneration.

| Artifact | SHA-256 |
|---|---|
| Upstream model | `2bc0a77812a15ff6ead003370f36149139ae2c1bb1faf779b00c00886cd919ef` |
| Uncompressed graph | `5eb524d5d293f14cdeae8486bc896a76b7748349ccbf10016fcd499b4dc52b13` |

[Upstream Apache-2.0 model and tokenizer at the pinned revision](https://huggingface.co/n24q02m/Qwen3-Reranker-0.6B-ONNX/tree/342a8a54de23bba03eb8cce5f5bef503edf18315)
