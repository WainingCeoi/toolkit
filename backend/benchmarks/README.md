# Watermark benchmark

Run from the repository root using the backend environment:

```sh
backend/.venv/bin/python backend/benchmarks/watermark_benchmark.py \
  --repeat 3 --output .local-docs/watermark-benchmark.json
```

The default uses cv2 and deterministic synthetic images: a repeating watermark,
a 3000×2000 enlargement, five different photos with a watermark stamped once in
each, a clean image, and an RGBA image. Fixture generation reuses
`backend/tests/watermark_fixtures.py`; run this from a source checkout.
Use repeated `--case` arguments to select cases, for example
`--case repeating-6mp --case single-batch`. No images or models are downloaded.

Each repeat runs in a fresh child process. JSON records runtime/package versions,
SHA-256 fingerprints of the engine, benchmark harness, shared fixture source,
every marked/original/mask input file, and the local LaMa checkpoint when used.
It also records device, dimensions, detection route, per-stage wall times,
and whole-child peak RSS on macOS/Linux (bytes). Fixture creation and process
startup are excluded from reported wall time; input decode, quality metrics, and
output encoding are included. Images in each batch are decoded once and held in
memory, so this measures the engine with an in-memory batch, not HTTP/ZIP overhead.
OpenCV defaults to one thread and child BLAS/OpenMP threads are limited to one;
`--threads` changes OpenCV concurrency. Compare repeats on the same machine with
the same settings, avoiding other heavy jobs.

Quality fields include mask recall, false-positive rate, selected fraction,
before/after RGB mean absolute error, preservation of pixels outside the dilated
mask, alpha preservation, and SHA-256 of the proposed mask and encoded output.
Recall is `null` when the truth has no watermark pixels. Detection time includes
the auto detector's safety probe when it falls back to texture detection; removal
time reuses that probe when possible. Protected/skipped images are measured as
unchanged. Synthetic results do not establish accuracy on real photographs.

To compare saved runs, require matching harness/fixture/input/model fingerprints,
package versions, device and settings. The engine fingerprint is expected to
change when comparing engine implementations. Match `case` and image position,
compare mask/output hashes for exact equality, and compare median `wall_seconds`
and `peak_rss_bytes` across repeats. A changed harness or dataset needs a fresh
baseline. Fingerprinting runs in the parent, outside measured child time/RSS.
Keep measurements in `.local-docs/`, which is gitignored.

For LaMa, provide an existing checkpoint explicitly:

```sh
backend/.venv/bin/python backend/benchmarks/watermark_benchmark.py \
  --case repeating --inpainter lama --model /absolute/path/big-lama.pt
```

LaMa also needs the backend's optional torch dependency. The benchmark refuses
LaMa without a local model (`WATERMARK_LAMA_MODEL` is also accepted), so it cannot
trigger the engine's automatic download. Each repeat includes a cold model load.

Optional local datasets use `--manifest /path/cases.json`. Relative image paths
resolve beside the manifest; inputs are only read:

```json
{
  "cases": [
    {
      "name": "supplier-batch",
      "images": [
        {"marked": "marked.png", "original": "original.png", "mask": "truth.png"}
      ]
    }
  ]
}
```

`original` and `mask` are optional. Originals must be aligned and have the same
EXIF-normalized dimensions as marked images. Truth masks are grayscale PNGs:
255 = watermark, 0 = clean, 128 = ignored antialiased fringe, at normalized image
dimensions. Without a mask, the benchmark derives a truth mask from RGB changes
greater than two levels against the original and labels this approximation in
`truth_source`; compression/exposure changes can make it misleading. Without
either source, accuracy and error metrics are `null`.
