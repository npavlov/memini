# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "onnx==1.23.2", "numpy==2.5.5", "protobuf==7.36.2",
#   "ml-dtypes==0.7.0", "typing-extensions==4.16.0",
# ]
# ///
"""Build the compact Linux execution graphs from the pinned Qwen3 reranker export."""

import argparse
import gzip
import hashlib
import mmap
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from onnx.external_data_helper import set_external_data

SOURCE_SHA256 = "2bc0a77812a15ff6ead003370f36149139ae2c1bb1faf779b00c00886cd919ef"
SOURCE_NAME = "model_yesno_quantized.onnx"
ACTIVATION_GRAPH_SHA256 = "7f8cf431d2c1a36b251357dbff046a2b28480e2ce5bf45b76c81def591f49111"
GRAPH_SHA256 = {
    "per-token": ACTIVATION_GRAPH_SHA256,
    "mixed": "3b40891c54cdea991c94856c7030e9769be55e0fd7ad53c2567fb5c0e3767a21",
}
BLOCK = 128


# Zero-based MatMulInteger ordinals in SOURCE_SHA256, in upstream graph order.
# The selection procedure and its input suite are documented in the adjacent README.
BLOCKWISE_PROJECTIONS = frozenset(
    {
        0,
        15,
        21,
        30,
        41,
        42,
        48,
        55,
        62,
        66,
        73,
        77,
        80,
        87,
        101,
        122,
        143,
        150,
        164,
        186,
        187,
        189,
        192,
        196,
    }
)


def per_token_quantization(graph):
    constants = {
        "token_axis": np.array([-1], np.int64),
        "token_epsilon": np.float32(1e-10),
        "token_scale": np.float32(1 / 127),
        "token_zero": np.uint8(128),
    }
    graph.initializer.extend(numpy_helper.from_array(v, k) for k, v in constants.items())
    replacements = {}
    nodes = []
    peaks = {}
    projection_peaks = {}
    quantizers = 0
    projections = 0
    for node in graph.node:
        if node.op_type == "DynamicQuantizeLinear":
            quantizers += 1
            (x,) = node.input
            quantized, scale, zero = node.output
            peak = x + "_peak"
            normalized = x + "_unit"
            nodes.extend(
                [
                    helper.make_node("Abs", [x], [x + "_abs"]),
                    helper.make_node(
                        "ReduceMax", [x + "_abs", "token_axis"], [x + "_max"], keepdims=1
                    ),
                    helper.make_node("Max", [x + "_max", "token_epsilon"], [peak]),
                    helper.make_node("Div", [x, peak], [normalized]),
                    helper.make_node(
                        "QuantizeLinear", [normalized, "token_scale", "token_zero"], [quantized]
                    ),
                ]
            )
            peaks[scale] = peak
            replacements[scale] = "token_scale"
            replacements[zero] = "token_zero"
            continue
        if node.op_type == "MatMulInteger":
            projections += 1
            if replacements.get(node.input[2]) != "token_zero":
                raise ValueError(f"unexpected activation zero point in {node.name}")
        if node.op_type == "Mul" and node.input[0] in peaks:
            projection_peaks[node.output[0]] = peaks[node.input[0]]
        for index, name in enumerate(node.input):
            if name in replacements:
                node.input[index] = replacements[name]
        if node.op_type == "Mul" and node.input[1] in projection_peaks:
            (output,) = node.output
            node.output[0] = output + "_scaled"
            nodes.extend(
                [
                    node,
                    helper.make_node(
                        "Mul", [node.output[0], projection_peaks[node.input[1]]], [output]
                    ),
                ]
            )
        else:
            nodes.append(node)
    if (quantizers, projections) != (113, 197):
        raise ValueError(f"unexpected number of quantizers/projections: {quantizers}/{projections}")
    graph.ClearField("node")
    graph.node.extend(nodes)


def blockwise_nodes(activation, weight, scale, outputs, shape):
    k, n = shape
    if k % BLOCK:
        raise ValueError(f"weight {weight} has an incomplete block")
    packed = weight + "_packed"
    dimensions = numpy_helper.from_array(
        np.array([n, k // BLOCK, BLOCK], np.int64), packed + "_dims"
    )
    scales = numpy_helper.from_array(np.array([n, k // BLOCK], np.int64), packed + "_scdims")
    nodes = [
        helper.make_node("Cast", [weight], [packed + "_u8"], to=TensorProto.UINT8),
        helper.make_node("BitwiseXor", [packed + "_u8", "xor_mask"], [packed + "_shifted"]),
        helper.make_node("Transpose", [packed + "_shifted"], [packed + "_trans"], perm=[1, 0]),
        helper.make_node("Reshape", [packed + "_trans", packed + "_dims"], [packed]),
        helper.make_node("Expand", [scale, packed + "_scdims"], [packed + "_scales"]),
        helper.make_node(
            "MatMulNBits",
            [activation, packed, packed + "_scales"],
            outputs,
            domain="com.microsoft",
            K=k,
            N=n,
            bits=8,
            block_size=BLOCK,
            accuracy_level=4,
        ),
    ]
    return nodes, (dimensions, scales)


def retain_outputs(graph, producers, tensors):
    """Keep only nodes reachable from graph outputs, in deterministic dependency order."""
    nodes = []
    constants = []
    seen = {value.name for value in graph.input}
    pending = [(value.name, False) for value in reversed(graph.output)]
    while pending:
        name, ready = pending.pop()
        if not name or name in seen:
            continue
        if name in tensors:
            constants.append(tensors[name])
            seen.add(name)
            continue
        node = producers[name]
        if ready:
            nodes.append(node)
            seen.update(node.output)
        else:
            pending.append((name, True))
            pending.extend((value, False) for value in reversed(node.input))
    graph.ClearField("node")
    graph.node.extend(nodes)
    graph.ClearField("initializer")
    graph.initializer.extend(constants)


def mixed_quantization(model):
    graph = model.graph
    tensors = {tensor.name: tensor for tensor in graph.initializer}
    producers = {output: node for node in graph.node for output in node.output}
    consumers = defaultdict(list)
    for node in graph.node:
        for name in node.input:
            consumers[name].append(node)

    replacements = []
    constants = [numpy_helper.from_array(np.uint8(128), "xor_mask")]
    projections = [node for node in graph.node if node.op_type == "MatMulInteger"]
    for index in sorted(BLOCKWISE_PROJECTIONS):
        node = projections[index]
        activation, weight, _, zero = node.input
        quant = producers[activation]
        (cast,) = consumers[node.output[0]]
        (rescale,) = consumers[cast.output[0]]
        combined = producers[next(name for name in rescale.input if name != cast.output[0])]
        operators = (quant.op_type, cast.op_type, rescale.op_type, combined.op_type)
        if operators != ("DynamicQuantizeLinear", "Cast", "Mul", "Mul"):
            raise ValueError(f"unexpected projection in {node.name}")
        if numpy_helper.to_array(tensors[zero]).item() != 0:
            raise ValueError(f"expected symmetric INT8 weights in {node.name}")
        weight_scale = next(name for name in combined.input if name != quant.output[1])
        nodes, shapes = blockwise_nodes(
            quant.input[0], weight, weight_scale, rescale.output, tensors[weight].dims
        )
        replacements.extend(nodes)
        constants.extend(shapes)

    per_token_quantization(graph)
    producers = {output: node for node in graph.node for output in node.output}
    producers.update((output, node) for node in replacements for output in node.output)
    tensors = {tensor.name: tensor for tensor in graph.initializer}
    tensors.update((tensor.name, tensor) for tensor in constants)
    retain_outputs(graph, producers, tensors)
    if not any(version.domain == "com.microsoft" for version in model.opset_import):
        model.opset_import.append(helper.make_opsetid("com.microsoft", 1))


def externalize(model, source, source_order):
    """Point each large initializer at its exact bytes in the verified upstream protobuf."""
    offset = 0
    # Mixed graph initializers are in traversal order; source offsets follow the upstream order.
    for tensor in sorted(
        model.graph.initializer, key=lambda t: source_order.get(t.name, len(source_order))
    ):
        data = tensor.raw_data
        if len(data) < 1024:
            continue
        index = source.find(data[:64], offset)
        if index < 0 or source[index : index + len(data)] != data:
            index = source.find(data, offset)
        if index < 0:
            raise ValueError(f"cannot locate original weights for {tensor.name}")
        set_external_data(tensor, location=SOURCE_NAME, offset=index, length=len(data))
        tensor.ClearField("raw_data")
        offset = index + len(data)

    for node in model.graph.node:
        node.ClearField("doc_string")
        node.ClearField("metadata_props")
    model.graph.ClearField("value_info")
    model.graph.ClearField("metadata_props")


def build(source, output, variant):
    if output.exists() and source.samefile(output):
        raise ValueError("the output must be separate from the upstream ONNX model")
    with source.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
            raise ValueError("input SHA-256 differs from the pinned upstream ONNX model")
        model = onnx.load_model_from_string(raw[:])
        source_order = {t.name: i for i, t in enumerate(model.graph.initializer)}
        if variant == "per-token":
            per_token_quantization(model.graph)
        else:
            mixed_quantization(model)
        onnx.checker.check_model(model)
        externalize(model, raw, source_order)
    graph = model.SerializeToString(deterministic=True)
    digest = hashlib.sha256(graph).hexdigest()
    if digest != GRAPH_SHA256[variant]:
        raise ValueError(f"generated graph SHA-256 differs from its pin: {digest}")
    with TemporaryDirectory(dir=output.parent) as directory:
        temporary = Path(directory) / "graph.gz"
        with (
            temporary.open("wb") as stream,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=stream, compresslevel=9, mtime=0
            ) as archive,
        ):
            archive.write(graph)
        temporary.replace(output)
    print(f"{digest}  uncompressed graph ({len(graph)} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="upstream model_yesno_quantized.onnx")
    parser.add_argument("output", type=Path, help="path for the .onnx.gz companion graph")
    parser.add_argument("--variant", choices=GRAPH_SHA256, default="per-token")
    args = parser.parse_args()
    build(args.source, args.output, args.variant)
