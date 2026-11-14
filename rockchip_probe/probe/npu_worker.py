"""Runs the official rknn-toolkit-lite2 resnet18 example in its own process.

The parent captures this process's stdout/stderr, which is where librknnrt
writes its runtime and driver version lines, and can kill it if the driver
hangs. Results are emitted as `RESULT_JSON:` lines; the last one wins, so a
kill during the loop still leaves the single-inference result behind.
"""

import argparse
import ast
import hashlib
import json
import sys
import time

import numpy as np
from PIL import Image

# sha256 of the RGB pixels OpenCV decodes from space_shuttle_224.jpg, which is
# what the official test.py feeds the model.
EXPECTED_INPUT_SHA256 = "f36e0734e1f08d3ef7d380ee568fee2daa12b6c0e26d7446da9278ccb66e35f2"


def load_input(path):
    return np.ascontiguousarray(np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8))


def load_labels(path):
    # The example ships labels as a Python module; read the literal instead of
    # importing (executing) it.
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "labels" for t in node.targets):
            return ast.literal_eval(node.value)
    return {}


def top5(output, labels):
    logits = np.asarray(output, dtype=np.float64).reshape(-1)
    probs = np.exp(logits - logits.max())
    probs /= probs.sum()
    return [
        {"index": int(i), "score": round(float(probs[i]), 6), "label": labels.get(int(i), "?")}
        for i in np.argsort(probs)[::-1][:5]
    ]


def emit(result):
    print("RESULT_JSON:" + json.dumps(result), flush=True)


def run(args):
    from rknnlite.api import RKNNLite

    labels = load_labels(args.labels)
    image = load_input(args.image)
    result = {
        "input_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
        "input_matches_official_decode": hashlib.sha256(image.tobytes()).hexdigest() == EXPECTED_INPUT_SHA256,
    }

    rk = RKNNLite()
    if rk.load_rknn(args.model) != 0:
        result["error"] = "load_rknn failed"
        emit(result)
        return 1
    t0 = time.perf_counter()
    if rk.init_runtime() != 0:
        result["error"] = "init_runtime failed"
        emit(result)
        return 1
    result["init_runtime_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    try:
        result["sdk_version"] = rk.get_sdk_version()
    except Exception as e:  # informational only
        result["sdk_version_error"] = repr(e)

    batch = np.expand_dims(image, 0)
    t0 = time.perf_counter()
    outputs = rk.inference(inputs=[batch])
    result["first_inference_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    if outputs is None:
        result["error"] = "inference returned no outputs"
        emit(result)
        return 1
    result["top5"] = top5(outputs[0], labels)
    emit(result)

    if args.loop_seconds > 0:
        top1 = result["top5"][0]["index"]
        times, mismatches = [], 0
        start = time.monotonic()
        deadline = start + args.loop_seconds
        while time.monotonic() < deadline:
            t0 = time.perf_counter()
            out = rk.inference(inputs=[batch])
            times.append((time.perf_counter() - t0) * 1000)
            if out is None or int(np.argmax(out[0])) != top1:
                mismatches += 1
        wall = time.monotonic() - start
        result["loop"] = {
            "seconds": round(wall, 2),
            "inferences": len(times),
            "per_second": round(len(times) / wall, 2) if wall else None,
            "ms_min": round(min(times), 2) if times else None,
            "ms_avg": round(sum(times) / len(times), 2) if times else None,
            "ms_max": round(max(times), 2) if times else None,
            "top1_mismatches": mismatches,
        }
        emit(result)

    rk.release()
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--check-input")
    p.add_argument("--model")
    p.add_argument("--image")
    p.add_argument("--labels")
    p.add_argument("--loop-seconds", type=float, default=0)
    args = p.parse_args()

    if args.check_input:
        digest = hashlib.sha256(load_input(args.check_input).tobytes()).hexdigest()
        print(f"decoded input sha256 {digest}")
        return 0 if digest == EXPECTED_INPUT_SHA256 else 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
