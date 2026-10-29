"""The layout model's CoreML compile is reused across processes (closure ticket 13).

On macOS the engine runs the layout model through onnxruntime's
CoreMLExecutionProvider. Without a model cache directory every new process
writes its own ``onnxruntime-<UUID>-<PID>-….model.mlmodel`` and
``.mlmodelc`` to ``$TMPDIR`` and the system compiles them for the neural
engine again. With the cache, the first process stores the compiled model in
a fixed directory under babeldoc's cache root and later processes load it
from there.

Two processes, one after the other, each load the layout model and detect
the layout of one page. The second one must create no compile files of its
own in ``$TMPDIR`` and must leave the cache directory as it was.

onnxruntime does not check a cache entry against the model, so the static
model carries a ``COREML_CACHE_KEY`` that changes with the weights and the
input size and stays the same for the same model (checked on a small model,
without creating a session).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

onnxruntime = pytest.importorskip("onnxruntime")
onnx = pytest.importorskip("onnx")

from babeldoc.docvision.doclayout import (
    COREML_CACHE_DIR,
    OnnxModel,
    _set_coreml_cache_key,
)

coreml_only = pytest.mark.skipif(
    sys.platform != "darwin"
    or "CoreMLExecutionProvider" not in onnxruntime.get_available_providers(),
    reason="the CoreML execution provider exists only on macOS",
)

PAGE = Path(__file__).parent / "fixtures" / "autodesign-p1.pdf"

# The child loads the layout model the way the paper task does, detects the
# layout of one page and, while the session is still alive, lists the
# onnxruntime files in $TMPDIR that carry its own process id (onnxruntime
# deletes them when the session is released, so the parent cannot see them
# after the child has exited).
CHILD = r"""
import json, os, sys, tempfile, time
import pymupdf
from babeldoc.docvision.doclayout import DocLayoutModel
from babeldoc.format.pdf.document_il.utils.raster_geometry import with_target_long_edge

start = time.monotonic()
model = DocLayoutModel.load_onnx()
load_seconds = time.monotonic() - start
document = pymupdf.open(sys.argv[1])
geometry = with_target_long_edge(document[0], 72, 1024, normalize_rotation=True)
result = model.predict(geometry.image[:, :, ::-1], geometry=geometry)[0]
marker = f"-{os.getpid()}-"
own = sorted(
    name
    for name in os.listdir(tempfile.gettempdir())
    if name.startswith("onnxruntime-") and marker in name
)
print(json.dumps({"boxes": len(result.boxes), "own_tmp_files": own, "load_seconds": load_seconds}))
"""


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    """Every file under the directory with its size and modification time."""
    if not directory.exists():
        return {}
    return {
        str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in directory.rglob("*")
        if path.is_file()
    }


def _run_child() -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", CHILD, str(PAGE)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


@coreml_only
def test_a_second_process_reuses_the_compiled_layout_model():
    cache = COREML_CACHE_DIR

    first = _run_child()
    after_first = _snapshot(cache)
    second = _run_child()
    after_second = _snapshot(cache)

    assert first["boxes"] > 0 and second["boxes"] == first["boxes"]
    assert second["own_tmp_files"] == [], (
        "the second process compiled the model again in $TMPDIR: "
        f"{second['own_tmp_files'][:4]}"
    )
    assert any(name.endswith("compiled_model.mlmodelc") or ".mlmodelc/" in name for name in after_first), (
        f"no compiled model in the cache directory {cache}"
    )
    assert after_second == after_first, "the second process changed the cache directory"


def _small_layout_model():
    """A one-convolution model with the layout model's dynamic input ``images``."""
    import numpy as np
    from onnx import TensorProto, helper, numpy_helper

    weights = np.arange(4 * 3 * 3 * 3, dtype=np.float32).reshape(4, 3, 3, 3) / 100
    images = helper.make_tensor_value_info(
        "images", TensorProto.FLOAT, ["batch", 3, "height", "width"]
    )
    output = helper.make_tensor_value_info("output", TensorProto.FLOAT, None)
    conv = helper.make_node("Conv", ["images", "weight"], ["output"], pads=[1, 1, 1, 1])
    graph = helper.make_graph(
        [conv], "layout", [images], [output], [numpy_helper.from_array(weights, "weight")]
    )
    return helper.make_model(graph)


def _key(model, imgsz: int) -> str:
    """The cache key a fresh process would give the model at this input size."""
    fresh = onnx.load_from_string(model.SerializeToString())
    return _set_coreml_cache_key(OnnxModel._make_static_model(fresh, imgsz))


def test_the_cache_key_changes_with_the_weights_and_the_input_size():
    model = _small_layout_model()
    one_byte_changed = _small_layout_model()
    raw = bytearray(one_byte_changed.graph.initializer[0].raw_data)
    raw[0] ^= 1
    one_byte_changed.graph.initializer[0].raw_data = bytes(raw)

    key = _key(model, 1024)

    assert key == _key(model, 1024)
    assert key != _key(model, 800)
    assert key != _key(one_byte_changed, 1024)
    assert key.isalnum() and len(key) <= 64  # onnxruntime ignores any other key
    assert key.endswith("ort" + onnxruntime.__version__.replace(".", ""))
