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
import json
import mmap
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
    "channel7": "64a83156c2b65a655d523d2fe54001da822325298ad42b5197ee736f41d0e849",
}
METADATA_SHA256 = "725464f6c5ff41460c60dad4cf192d9d3cddb0b773355c3c48f284be6d35bdc4"


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


def channel7_quantization(graph, source):
    tensors = {tensor.name: tensor for tensor in graph.initializer}
    replacements = {}
    scales = []
    placeholders = []
    metadata = []
    offset = 0
    for node in graph.node:
        if node.op_type != "MatMulInteger":
            continue
        _, weight, _, zero = node.input
        tensor = tensors[weight]
        if tensor.data_type != TensorProto.INT8 or numpy_helper.to_array(tensors[zero]).item() != 0:
            raise ValueError(f"expected a symmetric INT8 projection for {weight}")
        k, n = tensor.dims
        external = {entry.key: entry.value for entry in tensor.external_data}
        if external["location"] != SOURCE_NAME or int(external["length"]) != k * n:
            raise ValueError(f"invalid source offsets for {weight}")
        upstream_offset = int(external["offset"])
        upstream = np.ndarray((k, n), np.int8, buffer=source, offset=upstream_offset)
        peak = np.maximum(upstream.max(0).astype(np.int16), -upstream.min(0).astype(np.int16))
        factor = np.maximum(peak, 1).astype(np.float32) / np.float32(63)
        scale = weight.removesuffix("_quantized") + "_scale"
        name = scale + "_channel7"
        scales.append(numpy_helper.from_array(numpy_helper.to_array(tensors[scale]) * factor, name))
        replacements[scale] = name

        name = weight + "_7"
        projection = TensorProto(name=name, data_type=TensorProto.INT8, dims=(k, n))
        projection.data_location = TensorProto.EXTERNAL
        for key, value in (
            ("location", "memory-channel7.weights"),
            ("offset", offset),
            ("length", k * n),
        ):
            projection.external_data.add(key=key, value=str(value))
        placeholders.append(projection)
        metadata.append([name, upstream_offset, k, n])
        offset += k * n
        node.input[1] = name

    if len(metadata) != 197:
        raise ValueError(f"expected 197 projections, found {len(metadata)}")
    for node in graph.node:
        for i, name in enumerate(node.input):
            if name in replacements:
                node.input[i] = replacements[name]
    used = {name for node in graph.node for name in node.input}
    retained = [t for t in graph.initializer if t.name in used]
    graph.ClearField("initializer")
    graph.initializer.extend(retained + scales + placeholders)
    return json.dumps(metadata, separators=(",", ":")).encode()


def externalize(model, source):
    """Point each large initializer at its exact bytes in the verified upstream protobuf."""
    offset = 0
    for tensor in model.graph.initializer:
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


def build(source, output, variant, metadata_output=None):
    if variant == "channel7":
        if metadata_output is None or metadata_output.resolve() in (
            source.resolve(),
            output.resolve(),
        ):
            raise ValueError("channel7 requires a separate metadata output")
        if metadata_output.exists() and source.samefile(metadata_output):
            raise ValueError("the metadata output must be separate from the upstream ONNX model")
    if output.exists() and source.samefile(output):
        raise ValueError("the output must be separate from the upstream ONNX model")
    with source.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
            raise ValueError("input SHA-256 differs from the pinned upstream ONNX model")
        model = onnx.load_model_from_string(raw[:])
        per_token_quantization(model.graph)
        onnx.checker.check_model(model)
        externalize(model, raw)
        if variant == "channel7":
            metadata = channel7_quantization(model.graph, raw)
            if hashlib.sha256(metadata).hexdigest() != METADATA_SHA256:
                raise ValueError("generated metadata differs from its SHA-256 pin")
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
    if variant == "channel7":
        with TemporaryDirectory(dir=metadata_output.parent) as directory:
            temporary = Path(directory) / "metadata.json"
            temporary.write_bytes(metadata)
            temporary.replace(metadata_output)
    print(f"{digest}  uncompressed graph ({len(graph)} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="upstream model_yesno_quantized.onnx")
    parser.add_argument("output", type=Path, help="path for the .onnx.gz companion graph")
    parser.add_argument("--variant", choices=GRAPH_SHA256, default="per-token")
    parser.add_argument("--metadata", type=Path, help="channel7 initializer metadata output")
    args = parser.parse_args()
    build(args.source, args.output, args.variant, args.metadata)
