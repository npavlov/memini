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

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from onnx.external_data_helper import set_external_data

SOURCE_SHA256 = "2bc0a77812a15ff6ead003370f36149139ae2c1bb1faf779b00c00886cd919ef"
SOURCE_NAME = "model_yesno_quantized.onnx"
GRAPH_SHA256 = "54677185b94fc40db7ac1834c5e4b7fd7dc266501c233919cbd3fea96192bf77"


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
                    helper.make_node("Mul", [peak, "token_scale"], [x + "_per_scale"]),
                    helper.make_node("Div", [x, peak], [normalized]),
                    helper.make_node(
                        "QuantizeLinear", [normalized, "token_scale", "token_zero"], [quantized]
                    ),
                ]
            )
            replacements[scale] = x + "_per_scale"
            replacements[zero] = "token_zero"
            continue
        if node.op_type == "MatMulInteger":
            projections += 1
            if replacements.get(node.input[2]) != "token_zero":
                raise ValueError(f"unexpected activation zero point in {node.name}")
        for index, name in enumerate(node.input):
            if name in replacements:
                node.input[index] = replacements[name]
        nodes.append(node)
    if (quantizers, projections) != (113, 197):
        raise ValueError(f"unexpected number of quantizers/projections: {quantizers}/{projections}")
    graph.ClearField("node")
    graph.node.extend(nodes)


def final_token_mlp(graph):
    # The final attention layer needs all positions. Its MLP and the rest of the
    # score head compute only position -1 for the two next-token logits.
    (index,) = [i for i, node in enumerate(graph.node) if node.name == "node_add_9786"]
    residual = graph.node[index]
    if residual.op_type != "Add" or list(residual.input) != ["add_9488", "linear_192"]:
        raise ValueError("unexpected final attention residual")
    (source,) = residual.output
    if source != "add_9786":
        raise ValueError("unexpected final attention output")
    consumers = [node for node in graph.node if source in node.input]
    if {node.name for node in consumers} != {"node_pow_112", "node_mul_8794", "node_add_9836"}:
        raise ValueError("unexpected consumers of the final attention residual")
    (select,) = [node for node in graph.node if node.name == "node_select"]
    (position,) = [tensor for tensor in graph.initializer if tensor.name == "val_5085"]
    if (
        select.op_type != "Gather"
        or list(select.input) != ["mul_8834", position.name]
        or {a.name: a.i for a in select.attribute} != {"axis": 1}
        or numpy_helper.to_array(position).item() != -1
    ):
        raise ValueError("expected the upstream score head to select the last token")
    final = source + "_final_token"
    for node in consumers:
        for i, name in enumerate(node.input):
            if name == source:
                node.input[i] = final
    graph.initializer.append(helper.make_tensor("last_token_index", TensorProto.INT64, [1], [-1]))
    graph.node.insert(
        index + 1,
        helper.make_node(
            "Gather",
            [source, "last_token_index"],
            [final],
            name="last_token_after_attention",
            axis=1,
        ),
    )


def convert(model):
    per_token_quantization(model.graph)
    final_token_mlp(model.graph)
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
    with (
        output.open("wb") as stream,
        gzip.GzipFile(filename="", mode="wb", fileobj=stream, compresslevel=9, mtime=0) as archive,
    ):
        archive.write(graph)
    print(f"{digest}  uncompressed graph ({len(graph)} bytes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="upstream model_yesno_quantized.onnx")
    parser.add_argument("output", type=Path, help="path for the .onnx.gz companion graph")
    args = parser.parse_args()
    build(args.source, args.output)
