"""Watermark Remover API: upload staging, mask serving, and the inpaint job."""

from __future__ import annotations

import base64
import io
import subprocess
import sys
import threading
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


# --- Health ---


def test_watermark_health_reports_lama_and_the_resolved_device(client):
    body = client.get("/api/watermark/health").json()
    assert set(body) == {"lama", "device"}
    assert body["device"] in {"cpu", "mps", "cuda"}


def test_watermark_device_env_pins_the_device(client, monkeypatch):
    monkeypatch.setenv("WATERMARK_DEVICE", "cpu")
    assert client.get("/api/watermark/health").json()["device"] == "cpu"


def test_creating_the_app_never_imports_torch():
    code = (
        "import sys; from toolkit_api.main import create_app; create_app(); "
        "sys.exit(1 if 'torch' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# --- Upload / staging ---


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


# --- Working copy + auto-mask endpoints ---


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


# --- Run ---


def test_run_inpaints_only_the_masked_pixels(client):
    # The square is faint (+32) so the destruction guard does not refuse it.
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

    assert snap["result"]["batch_id"] == batch["batch_id"]
    zip_download = client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    assert snap["result"]["filename"] == "cleaned_images.zip"
    with zipfile.ZipFile(io.BytesIO(zip_download.content)) as archive:
        assert archive.namelist() == ["square.png"]
        cleaned = np.asarray(Image.open(io.BytesIO(archive.read("square.png"))))
    assert cleaned[28, 40].mean() < 145
    assert np.array_equal(cleaned[:10, :10], rgb[:10, :10])


def test_the_cv2_run_writes_the_same_pixels_as_a_plain_removal(client):
    from watermark.inpaint import inpaint_cv2
    from watermark.pipeline import DEFAULT_DILATE_PX, remove_watermark

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
    snap = wait_for_job(client, resp.json()["job_id"])
    mask = np.zeros((60, 80), np.uint8)
    mask[20:36, 30:50] = 255
    expected = remove_watermark(rgb, mask, inpaint_cv2, DEFAULT_DILATE_PX)

    download = client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        cleaned = np.asarray(Image.open(io.BytesIO(archive.read("square.png"))))
    assert np.array_equal(cleaned, expected)


def test_a_transparent_upload_keeps_its_transparency(client):
    rgba = np.full((60, 80, 4), 255, np.uint8)
    rgba[:, :, :3] = 128
    rgba[20:36, 30:50, :3] = 160
    rgba[0:10, 0:10, 3] = 0  # a cut-out corner, as a logo or sticker has
    buffer = io.BytesIO()
    Image.fromarray(rgba).save(buffer, format="PNG")

    batch = upload(client, ("logo.png", buffer.getvalue())).json()
    image = batch["images"][0]
    working = client.get(f"/api/watermark/{batch['batch_id']}/{image['id']}/image")
    assert np.asarray(Image.open(io.BytesIO(working.content)))[0:10, 0:10, 3].max() == 0

    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {image["id"]: mask_b64(80, 60, box=(30, 20, 50, 36))},
        },
    )
    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["result"]["done"] == ["logo.png"]
    download = client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        cleaned = Image.open(io.BytesIO(archive.read("logo.png")))
    assert cleaned.mode == "RGBA"
    assert np.asarray(cleaned)[0:10, 0:10, 3].max() == 0


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
    batch = upload(
        client,
        ("first.png", png_bytes((20, 10))),
        ("second.png", png_bytes((20, 10))),
    ).json()

    def exploding_replace(artifact_id, src):
        # replace_file is the per-image zip republish, outside the per-file try/except.
        raise MemoryError("out of memory inpainting a huge image")

    monkeypatch.setattr(app_state.artifacts, "replace_file", exploding_replace)

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
    assert snap["result"]["done"] == ["first.png"]
    download = client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert archive.namelist() == ["first.png"]


def test_results_are_published_before_the_batch_ends(client, app_state):
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
    assert seen and seen[0]["done"] == ["a.png"]


def test_an_empty_mask_is_skipped_not_written_back(client):
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
    assert "artifact_id" not in snap["result"]


def test_an_empty_mask_on_a_visible_repeat_is_protected(client, monkeypatch):
    from toolkit_api.routers import watermark as router

    monkeypatch.setattr(router, "repeating_evidence", lambda rgb: True)
    batch = upload(client, ("sheet.png", png_bytes((20, 10)))).json()
    image = batch["images"][0]
    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {image["id"]: mask_b64(20, 10)},
        },
    )
    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["protected"] == ["sheet.png"]
    assert snap["result"]["skipped"] == []


def test_the_spool_is_staged_inside_the_batch_directory(client, app_state, monkeypatch):
    from toolkit_api.routers import watermark as router
    from watermark.inpaint import inpaint_cv2

    batch = upload(client, ("a.png", png_bytes((20, 10)))).json()
    image = batch["images"][0]
    batch_dir = app_state.watermarks.get(batch["batch_id"])["dir"]
    seen = []

    def spying_inpaint(rgb, mask):
        seen.append([p.name for p in batch_dir.iterdir() if p.is_dir()])
        return inpaint_cv2(rgb, mask)

    monkeypatch.setattr(router, "get_inpainter", lambda name: spying_inpaint)
    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {image["id"]: mask_b64(20, 10, box=(0, 0, 5, 5))},
        },
    )
    wait_for_job(client, resp.json()["job_id"])
    # In the batch dir the store already sweeps, not a temp dir nothing cleans.
    assert seen and seen[0], "the spool was staged outside the batch directory"
    assert [p for p in batch_dir.iterdir() if p.is_dir()] == []


def test_cancelling_stops_partway_through_a_big_image(client, app_state, monkeypatch):
    from toolkit_api.routers import watermark as router
    from watermark.inpaint import inpaint_cv2

    wide = np.full((80, 1500, 3), 130, np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(wide).save(buffer, format="PNG")
    batch = upload(client, ("wide.png", buffer.getvalue())).json()
    image = batch["images"][0]

    running = []
    real_submit = app_state.jobs.submit

    def spy_submit(tool, names, worker):
        def wrapper(job):
            running.append(job)
            return worker(job)

        return real_submit(tool, names, wrapper)

    app_state.jobs.submit = spy_submit

    tiles = []

    def cancelling_inpaint(rgb, mask):
        tiles.append(rgb.shape)
        app_state.jobs.cancel(running[0].id)
        return inpaint_cv2(rgb, mask)

    monkeypatch.setattr(router, "get_inpainter", lambda name: cancelling_inpaint)

    mask = np.zeros((80, 1500), np.uint8)
    mask[10:30, 10:30] = 255
    mask[10:30, 800:820] = 255  # a second tile, so the cancel lands mid-image
    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "cv2",
            "masks": {image["id"]: base64.b64encode(imgio.encode_png(mask)).decode()},
        },
    )
    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["state"] == "cancelled"
    assert len(tiles) == 1, f"inpainted {len(tiles)} tiles after the cancel"
    assert snap["result"]["done"] == []


def test_lama_is_loaded_while_the_loading_message_is_up(client, app_state, monkeypatch):
    from toolkit_api.routers import watermark as router
    from watermark.inpaint import inpaint_cv2

    running = []
    real_submit = app_state.jobs.submit

    def spy_submit(tool, names, worker):
        def wrapper(job):
            running.append(job)
            return worker(job)

        return real_submit(tool, names, wrapper)

    app_state.jobs.submit = spy_submit
    messages = []

    class FakeLama:
        def load(self):
            messages.append(running[0].snapshot()["message"])

        def __call__(self, rgb, mask):
            return inpaint_cv2(rgb, mask)

    monkeypatch.setattr(router, "lama_available", lambda: True)
    monkeypatch.setattr(router, "get_inpainter", lambda name: FakeLama())
    batch = upload(client, ("a.png", png_bytes((20, 10)))).json()
    image = batch["images"][0]
    resp = client.post(
        "/api/watermark/run",
        json={
            "batch_id": batch["batch_id"],
            "inpainter": "lama",
            "masks": {image["id"]: mask_b64(20, 10, box=(0, 0, 5, 5))},
        },
    )
    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert messages and "Loading LaMa" in messages[0], (
        f"the model was loaded under the message {messages!r}"
    )


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


# --- Batch store lifecycle ---


def test_expired_batches_are_swept_on_access(tmp_path):
    store = WatermarkBatches(tmp_path / "wm", ttl=0.0)
    batch = store.create([("a.png", png_bytes(), 64, 48)])
    assert store.get(batch["id"]) is None
    assert not batch["dir"].exists()


def test_using_a_batch_keeps_it_alive(tmp_path):
    store = WatermarkBatches(tmp_path / "wm", ttl=0.05)
    batch = store.create([("a.png", png_bytes(), 64, 48)])
    for _ in range(4):
        time.sleep(0.02)
        assert store.get(batch["id"]) is not None
    time.sleep(0.08)
    assert store.get(batch["id"]) is None


def test_a_pinned_batch_survives_its_own_expiry(tmp_path):
    store = WatermarkBatches(tmp_path / "wm", ttl=0.0)
    batch = store.create([("a.png", png_bytes(), 64, 48)])
    with store.pin(batch["id"]):
        store.create([("b.png", png_bytes(), 64, 48)])  # sweeps on create
        assert batch["images"][0]["path"].is_file()
    assert store.get(batch["id"]) is None


def test_concurrent_callers_collect_the_batch_marks_once(tmp_path):
    store = WatermarkBatches(tmp_path / "wm")
    batch = store.create([("a.png", png_bytes(), 64, 48)])
    entered = threading.Event()
    calls = []

    def collect(paths):
        calls.append(len(paths))
        entered.set()
        time.sleep(0.1)
        return ["mark"]

    first = threading.Thread(target=store.marks, args=(batch["id"], collect))
    first.start()
    assert entered.wait(2.0)
    assert store.marks(batch["id"], collect) == ["mark"]
    first.join(2.0)
    assert calls == [1], "the second caller recomputed the whole batch"


def test_startup_clears_leftovers_from_a_previous_process(tmp_path):
    root = tmp_path / "wm"
    (root / "abc123def456").mkdir(parents=True)
    (root / "abc123def456" / "img.png").write_bytes(b"old")
    store = WatermarkBatches(root)
    assert list(root.iterdir()) == []
    assert store.get("abc123def456") is None


def test_startup_never_deletes_anything_it_did_not_create(tmp_path):
    root = tmp_path / "wm"
    (root / "My Holiday Photos").mkdir(parents=True)
    (root / "My Holiday Photos" / "beach.jpg").write_bytes(b"precious")
    (root / "notes.txt").write_bytes(b"precious")
    WatermarkBatches(root)
    assert (root / "My Holiday Photos" / "beach.jpg").read_bytes() == b"precious"
    assert (root / "notes.txt").read_bytes() == b"precious"
