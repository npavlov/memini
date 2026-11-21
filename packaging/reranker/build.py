# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "onnx==1.23.2", "numpy==2.5.5", "protobuf==7.36.2",
#   "ml-dtypes==0.7.0", "typing-extensions==4.16.0",
# ]
# ///
"""Build the compact per-token INT8 graph from the pinned Qwen3 reranker export."""

import argparse
import gzip
import hashlib
import mmap
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import onnx
from onnx import helper, numpy_helper
from onnx.external_data_helper import set_external_data

SOURCE_SHA256 = "2bc0a77812a15ff6ead003370f36149139ae2c1bb1faf779b00c00886cd919ef"
SOURCE_NAME = "model_yesno_quantized.onnx"
GRAPH_SHA256 = "7f8cf431d2c1a36b251357dbff046a2b28480e2ce5bf45b76c81def591f49111"


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


def convert(model):
    per_token_quantization(model.graph)
    onnx.checker.check_model(model)


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


def build(source, output):
    if output.exists() and source.samefile(output):
        raise ValueError("the output must be separate from the upstream ONNX model")
    with source.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
            raise ValueError("input SHA-256 differs from the pinned upstream ONNX model")
        model = onnx.load_model_from_string(raw[:])
        convert(model)
        externalize(model, raw)
    graph = model.SerializeToString(deterministic=True)
    digest = hashlib.sha256(graph).hexdigest()
    if digest != GRAPH_SHA256:
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
    args = parser.parse_args()
    build(args.source, args.output)
