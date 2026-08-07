"""Watermark Remover API: upload staging, mask serving, and the inpaint job.

The engine runs for real on the cv2 path — it is fast and pure, so faking it
would test less for no speed win. Images are tiny synthetic PNGs; masks are
drawn as arrays and PNG-encoded, exactly what the canvas editor exports.
"""

from __future__ import annotations

import base64
import io
import subprocess
import sys
import time
import zipfile

import numpy as np
from PIL import Image

from toolkit_api.jobs import FINISHED_STATES
from toolkit_api.watermarks import WatermarkBatches
from watermark import imgio


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in FINISHED_STATES:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def png_bytes(size=(64, 48), color=(120, 130, 140)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def upload(client, *files):
    return client.post(
        "/api/watermark/batch",
        files=[("files", (name, data, "image/png")) for name, data in files],
    )


def mask_b64(width, height, box=None):
    """A black/white mask PNG as the canvas editor would export it."""
    mask = np.zeros((height, width), np.uint8)
    if box is not None:
        left, top, right, bottom = box
        mask[top:bottom, left:right] = 255
    return base64.b64encode(imgio.encode_png(mask)).decode()


# =========================================================================
# Health
# =========================================================================


def test_watermark_health_reports_lama_and_the_resolved_device(client):
    body = client.get("/api/watermark/health").json()
    assert set(body) == {"lama", "device"}
    # Whatever this machine has — an accelerator is picked automatically, so
    # pinning "cpu" here would fail on any Mac with MPS.
    assert body["device"] in {"cpu", "mps", "cuda"}


def test_watermark_device_env_pins_the_device(client, monkeypatch):
    monkeypatch.setenv("WATERMARK_DEVICE", "cpu")
    assert client.get("/api/watermark/health").json()["device"] == "cpu"


def test_creating_the_app_never_imports_torch():
    # The lazy-ML contract, at app level: every router (watermark included)
    # is imported by create_app, and none of that may pull in torch.
    code = (
        "import sys; from toolkit_api.main import create_app; create_app(); "
        "sys.exit(1 if 'torch' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# =========================================================================
# Upload / staging
# =========================================================================


def test_batch_stages_images_and_reports_dimensions(client):
    resp = upload(
        client, ("a.png", png_bytes((64, 48))), ("b.png", png_bytes((30, 20)))
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_id"]
    assert [(i["name"], i["width"], i["height"]) for i in body["images"]] == [
        ("a.png", 64, 48),
        ("b.png", 30, 20),
    ]


def test_batch_dedupes_identical_filenames(client):
    resp = upload(client, ("a.png", png_bytes()), ("a.png", png_bytes()))
    names = [i["name"] for i in resp.json()["images"]]
    assert len(set(names)) == 2 and "a.png" in names


def test_batch_without_files_is_400(client):
    resp = client.post("/api/watermark/batch")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Please select at least one image first."


def test_batch_rejects_more_than_twenty_images(client):
    files = [(f"img_{i}.png", png_bytes((8, 8))) for i in range(21)]
    resp = upload(client, *files)
    assert resp.status_code == 400
    assert "limit is 20" in resp.json()["detail"]


def test_batch_rejects_non_image_types(client):
    resp = upload(client, ("notes.txt", b"hello"))
    assert resp.status_code == 400
    assert "Unsupported file type: notes.txt" in resp.json()["detail"]


def test_batch_rejects_bytes_that_do_not_decode(client):
    resp = upload(client, ("broken.png", b"not really a png"))
    assert resp.status_code == 400
    assert "Could not read broken.png" in resp.json()["detail"]


def test_batch_sanitizes_traversal_filenames_to_basenames(client):
    resp = upload(client, ("../../escape.png", png_bytes()))
    assert resp.status_code == 200
    assert resp.json()["images"][0]["name"] == "escape.png"


# =========================================================================
# Working copy + auto-mask endpoints
# =========================================================================


def test_working_copy_is_served_as_png_at_native_size(client):
    batch = upload(client, ("a.png", png_bytes((40, 30)))).json()
    image_id = batch["images"][0]["id"]
    resp = client.get(f"/api/watermark/{batch['batch_id']}/{image_id}/image")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(resp.content)).size == (40, 30)


def test_auto_mask_matches_image_dimensions(client):
    batch = upload(client, ("a.png", png_bytes((40, 30)))).json()
    image_id = batch["images"][0]["id"]
    resp = client.get(
        f"/api/watermark/{batch['batch_id']}/{image_id}/mask?sensitivity=80"
    )
    assert resp.status_code == 200
    mask = Image.open(io.BytesIO(resp.content))
    assert mask.size == (40, 30)


def test_out_of_range_sensitivity_is_rejected(client):
    batch = upload(client, ("a.png", png_bytes())).json()
    image_id = batch["images"][0]["id"]
    url = f"/api/watermark/{batch['batch_id']}/{image_id}/mask"
    assert client.get(f"{url}?sensitivity=101").status_code == 422
    assert client.get(f"{url}?sensitivity=-1").status_code == 422


def test_unknown_batch_and_image_are_404(client):
    assert client.get("/api/watermark/nope/nada/image").status_code == 404
    assert client.get("/api/watermark/nope/nada/mask").status_code == 404


# =========================================================================
# Run
# =========================================================================


def test_run_inpaints_only_the_masked_pixels(client):
    # Flat gray with a lighter square; the mask covers the square. Inpainting
    # must pull the square toward gray and leave far-away pixels untouched.
    #
    # The square is 32 grey levels above the ground, not the 127 it used to be,
    # because this tool is for SEMI-TRANSPARENT overlays and the run now refuses
    # an image when the fill would rewrite it beyond recognition (see
    # would_destroy_content). A solid white block tripped that guard, which is
    # the guard working: erasing it really would be a drastic rewrite. This test
    # is about compositing only masked pixels, so it uses a mark of the kind the
    # tool is actually meant to remove.
    rgb = np.full((60, 80, 3), 128, np.uint8)
    rgb[20:36, 30:50] = 160
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="PNG")

    batch = upload(client, ("square.png", buffer.getvalue())).json()
    image = batch["images"][0]
    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {image["id"]: mask_b64(80, 60, box=(30, 20, 50, 36))},
        },
    )
    assert resp.status_code == 200

    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["done"] == ["square.png"]
    assert snap["result"]["failed"] == []
    assert snap["items"][0] == {
        "name": "square.png",
        "pct": 100,
        "state": "done",
        "error": None,
    }

    # The result carries everything the results view needs on its own, so it
    # still renders after the page has been unmounted and remounted.
    assert snap["result"]["batch_id"] == batch["batch_id"]
    file_entry = snap["result"]["files"][0]
    assert file_entry["image_id"] == image["id"]
    download = client.get(f"/api/artifacts/{file_entry['artifact_id']}")
    cleaned = np.asarray(Image.open(io.BytesIO(download.content)))
    assert cleaned[28, 40].mean() < 145  # square filled from its gray surround
    assert np.array_equal(cleaned[:10, :10], rgb[:10, :10])  # far corner intact

    zip_download = client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    assert snap["result"]["filename"] == "cleaned_images.zip"
    with zipfile.ZipFile(io.BytesIO(zip_download.content)) as archive:
        assert archive.namelist() == ["square.png"]


def test_run_processes_only_images_that_got_a_mask(client):
    batch = upload(
        client, ("a.png", png_bytes((20, 10))), ("b.png", png_bytes((20, 10)))
    ).json()
    chosen = batch["images"][1]
    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {chosen["id"]: mask_b64(20, 10, box=(0, 0, 5, 5))},
        },
    )
    snap = wait_for_job(client, resp.json()["job_id"])
    assert [item["name"] for item in snap["items"]] == ["b.png"]
    assert snap["result"]["done"] == ["b.png"]


def test_a_wrong_size_mask_fails_that_item_and_spares_the_rest(client):
    batch = upload(
        client, ("good.png", png_bytes((20, 10))), ("bad.png", png_bytes((30, 40)))
    ).json()
    good, bad = batch["images"]
    right = mask_b64(20, 10, box=(0, 0, 5, 5))
    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            # The bad.png mask has good.png's dimensions.
            "masks": {good["id"]: right, bad["id"]: right},
        },
    )
    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["done"] == ["good.png"]
    assert snap["result"]["failed"][0][0] == "bad.png"
    assert "Mask is 20×10" in snap["result"]["failed"][0][1]
    states = {item["name"]: item["state"] for item in snap["items"]}
    assert states == {"good.png": "done", "bad.png": "failed"}


def test_colliding_output_stems_are_deduped_in_the_zip(client):
    # a.png and a.jpg both clean to "a.png" — the zip must keep two entries.
    jpeg = io.BytesIO()
    Image.new("RGB", (20, 10), "gray").save(jpeg, format="JPEG")
    batch = upload(
        client, ("a.png", png_bytes((20, 10))), ("a.jpg", jpeg.getvalue())
    ).json()
    masks = {
        image["id"]: mask_b64(20, 10, box=(0, 0, 5, 5)) for image in batch["images"]
    }
    resp = client.post(
        "/api/watermark/run",
        json={"batch_id": batch["batch_id"], "inpainter": "cv2", "masks": masks},
    )
    snap = wait_for_job(client, resp.json()["job_id"])
    names = snap["result"]["done"]
    assert len(names) == 2 and len(set(names)) == 2


def test_a_crash_midway_still_hands_back_what_finished(client, app_state, monkeypatch):
    # The real report: 7 of 8 images cleaned, the 8th took the run down, and
    # the 7 were lost. Results are published per image, so they survive.
    batch = upload(
        client,
        ("first.png", png_bytes((20, 10))),
        ("second.png", png_bytes((20, 10))),
    ).json()
    real_put = app_state.artifacts.put_bytes
    calls = {"n": 0}

    def exploding_put(filename, content, media_type):
        calls["n"] += 1
        if calls["n"] == 2:  # second image, outside the per-file try/except
            raise MemoryError("out of memory inpainting a huge image")
        return real_put(filename, content, media_type)

    monkeypatch.setattr(app_state.artifacts, "put_bytes", exploding_put)

    masks = {
        image["id"]: mask_b64(20, 10, box=(0, 0, 5, 5)) for image in batch["images"]
    }
    resp = client.post(
        "/api/watermark/run",
        json={"batch_id": batch["batch_id"], "inpainter": "cv2", "masks": masks},
    )
    snap = wait_for_job(client, resp.json()["job_id"])

    assert snap["state"] == "failed"
    assert "out of memory" in snap["error"]
    # ...and the first image is still there, downloadable.
    assert snap["result"]["done"] == ["first.png"]
    harvested = snap["result"]["files"][0]
    assert client.get(f"/api/artifacts/{harvested['artifact_id']}").status_code == 200


def test_results_are_published_before_the_batch_ends(client, app_state):
    # Not just on failure: a long batch should be able to hand back image 1
    # while image 2 is still running.
    batch = upload(client, ("a.png", png_bytes((20, 10)))).json()
    image = batch["images"][0]
    seen = []

    real_submit = app_state.jobs.submit

    def spy_submit(tool, names, worker):
        def wrapper(job):
            out = worker(job)
            seen.append(job.snapshot()["result"])
            return out

        return real_submit(tool, names, wrapper)

    app_state.jobs.submit = spy_submit
    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {image["id"]: mask_b64(20, 10, box=(0, 0, 5, 5))},
        },
    )
    wait_for_job(client, resp.json()["job_id"])
    # The worker had already published before returning, so the registry saw a
    # result while the job was still "running".
    assert seen and seen[0]["done"] == ["a.png"]


def test_an_empty_mask_is_skipped_not_written_back(client):
    # The detector declines on images with no recoverable watermark, which
    # arrives here as an all-black mask. Inpainting nothing and calling the
    # result "cleaned" would hand back a no-op dressed as a success.
    batch = upload(client, ("plain.png", png_bytes((20, 10)))).json()
    image = batch["images"][0]
    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {image["id"]: mask_b64(20, 10)},  # no box = nothing marked
        },
    )
    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["skipped"] == ["plain.png"]
    assert snap["result"]["done"] == []
    assert snap["result"]["files"] == []
    assert "artifact_id" not in snap["result"]


def test_run_rejects_an_unknown_inpainter(client):
    batch = upload(client, ("a.png", png_bytes())).json()
    resp = client.post(
        "/api/watermark/run",
        json={"batch_id": batch["batch_id"], "inpainter": "photoshop", "masks": {}},
    )
    assert resp.status_code == 400
    assert "Unknown inpainter" in resp.json()["detail"]


def test_run_refuses_lama_without_torch_and_names_the_fix(client, monkeypatch):
    from toolkit_api.routers import watermark as watermark_router

    monkeypatch.setattr(watermark_router, "lama_available", lambda: False)
    batch = upload(client, ("a.png", png_bytes())).json()
    resp = client.post(
        "/api/watermark/run",
        json={"batch_id": batch["batch_id"], "inpainter": "lama", "masks": {}},
    )
    assert resp.status_code == 400
    assert "uv sync --extra watermark" in resp.json()["detail"]


def test_run_validation_errors(client):
    batch = upload(client, ("a.png", png_bytes((20, 10)))).json()
    image_id = batch["images"][0]["id"]

    unknown_batch = client.post(
        "/api/watermark/run",
        json={"batch_id": "nope", "inpainter": "cv2", "masks": {"x": "AA=="}},
    )
    assert unknown_batch.status_code == 404

    empty_masks = client.post(
        "/api/watermark/run",
        json={"batch_id": batch["batch_id"], "inpainter": "cv2", "masks": {}},
    )
    assert empty_masks.status_code == 400
    assert "No masks" in empty_masks.json()["detail"]

    unknown_image = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {"stranger": "AA=="},
        },
    )
    assert unknown_image.status_code == 400
    assert "Unknown image id" in unknown_image.json()["detail"]

    bad_base64 = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {image_id: "%%% not base64 %%%"},
        },
    )
    assert bad_base64.status_code == 400
    assert "not valid base64" in bad_base64.json()["detail"]


# =========================================================================
# Batch store lifecycle
# =========================================================================


def test_expired_batches_are_swept_on_access(tmp_path):
    store = WatermarkBatches(tmp_path / "wm", ttl=0.0)
    batch = store.create([("a.png", png_bytes(), 64, 48)])
    assert store.get(batch["id"]) is None
    assert not batch["dir"].exists()


def test_using_a_batch_keeps_it_alive(tmp_path):
    # Expiry is measured from last use, not from creation: a correction
    # session is allowed to take longer than the TTL.
    store = WatermarkBatches(tmp_path / "wm", ttl=0.05)
    batch = store.create([("a.png", png_bytes(), 64, 48)])
    for _ in range(4):
        time.sleep(0.02)
        assert store.get(batch["id"]) is not None
    time.sleep(0.08)
    assert store.get(batch["id"]) is None


def test_a_pinned_batch_survives_its_own_expiry(tmp_path):
    # A run reads its images lazily; the batch must not be swept between two
    # images of that run just because it aged out mid-flight.
    store = WatermarkBatches(tmp_path / "wm", ttl=0.0)
    batch = store.create([("a.png", png_bytes(), 64, 48)])
    with store.pin(batch["id"]):
        store.create([("b.png", png_bytes(), 64, 48)])  # sweeps on create
        assert batch["images"][0]["path"].is_file()
    assert store.get(batch["id"]) is None  # ...and goes as soon as it is idle


def test_startup_clears_leftovers_from_a_previous_process(tmp_path):
    root = tmp_path / "wm"
    (root / "abc123def456").mkdir(parents=True)
    (root / "abc123def456" / "img.png").write_bytes(b"old")
    store = WatermarkBatches(root)
    assert list(root.iterdir()) == []
    assert store.get("abc123def456") is None


def test_startup_never_deletes_anything_it_did_not_create(tmp_path):
    # The root is derived from SUB_DB_PATH, which the user can point anywhere,
    # so a blanket rmtree of it could take a real folder with it. Only
    # batch-id-shaped directories are ever swept.
    root = tmp_path / "wm"
    (root / "My Holiday Photos").mkdir(parents=True)
    (root / "My Holiday Photos" / "beach.jpg").write_bytes(b"precious")
    (root / "notes.txt").write_bytes(b"precious")
    WatermarkBatches(root)
    assert (root / "My Holiday Photos" / "beach.jpg").read_bytes() == b"precious"
    assert (root / "notes.txt").read_bytes() == b"precious"
