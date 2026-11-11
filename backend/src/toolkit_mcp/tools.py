"""Typed toolbox operations. Business rules and state live behind the HTTP API."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .client import ToolkitClient
from .common import annotations


def register_tools(mcp: MCPServer, api: ToolkitClient) -> None:
    """Register explicit, reviewable HTTP mappings; never import backend engines."""

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def magnet_get_config() -> dict[str, Any]:
        """Read the backend's source website and last watched video cutoff."""
        return await api.request("GET", "/magnet/config")

    @mcp.tool(annotations=annotations(destructive=True, idempotent=True))
    async def magnet_set_config(website_url: str, cutoff_video: str) -> dict[str, Any]:
        """Save source/cutoff URLs to backend/.env, replacing existing settings.

        Both arguments are required; empty strings clear values. Changes apply to
        the shared backend immediately. Automatic scraping needs both URLs.
        """
        return await api.request(
            "PUT",
            "/magnet/config",
            json={"website_url": website_url, "cutoff_video": cutoff_video},
        )

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def magnet_auto_start(
        start_page: Annotated[int, Field(ge=1)] = 1,
    ) -> dict[str, Any]:
        """Start scraping unwatched pages using saved settings; return job_id.

        Poll job_status for links and errors. Successful runs update the saved
        cutoff in backend/.env. job_cancel is cooperative; completed fetches and
        already saved settings are not rolled back.
        """
        return await api.request(
            "POST", "/magnet/auto", json={"start_page": start_page}
        )

    @mcp.tool(annotations=annotations(open_world=True))
    async def magnet_manual_start(urls: list[str]) -> dict[str, Any]:
        """Fetch magnet links from explicit video-page URLs; return job_id.

        Poll job_status for successful links, failures, and duplicate counts.
        job_cancel stops further work cooperatively. Does not change the cutoff.
        """
        return await api.request("POST", "/magnet/manual", json={"urls": urls})

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def magnet_dedupe(links: list[str]) -> dict[str, Any]:
        """Return exact-string unique magnet links in original order and count."""
        return await api.request("POST", "/magnet/dedupe", json={"links": links})

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def remux_scan(folder: str) -> dict[str, Any]:
        """List remuxable videos in a backend-host folder; select returned paths.

        Use explicit absolute paths, not an OS file picker. This does not start
        FFmpeg or create a job. Track indices are chosen in remux_start.
        """
        return await api.request("POST", "/remux/scan", json={"folder": folder})

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def remux_match_subtitles(
        sub_folder: str, selected: list[str]
    ) -> dict[str, Any]:
        """Match external subtitles for selected backend-host video paths.

        Returns matches of video to subtitle (possibly null). Pass that mapping
        to remux_start.external_sub_map with use_external_sub=true.
        """
        return await api.request(
            "POST",
            "/remux/subtitles",
            json={"sub_folder": sub_folder, "selected": selected},
        )

    @mcp.tool(annotations=annotations(destructive=True))
    async def remux_start(
        selected: list[str],
        out_folder: str,
        include_video: bool = True,
        video_index: int = 0,
        multi_audio: bool = False,
        audio_value: str = "0",
        include_subtitle: bool = True,
        subtitle_index: int = 0,
        sub_lang: str = "chi",
        use_external_sub: bool = False,
        external_sub_map: dict[str, str | None] | None = None,
        max_workers: Annotated[int, Field(ge=1, le=8)] = 4,
    ) -> dict[str, Any]:
        """Start lossless FFmpeg remux of selected backend-host paths; job_id.

        Output files use the input basenames in absolute out_folder. Existing
        output files can be overwritten; source aliases are rejected. Track
        indices are zero-based by stream type. audio_value is one index, or
        comma-separated indices with multi_audio=true; empty with multi_audio
        selects no audio. External subtitles use video-path keys. Poll job_status
        for counts and output folder; job_cancel stops workers/FFmpeg but does
        not undo completed outputs. FFmpeg must be available to the backend.
        """
        return await api.request(
            "POST",
            "/remux/start",
            json={
                "selected": selected,
                "out_folder": out_folder,
                "include_video": include_video,
                "video_index": video_index,
                "multi_audio": multi_audio,
                "audio_value": audio_value,
                "include_subtitle": include_subtitle,
                "subtitle_index": subtitle_index,
                "sub_lang": sub_lang,
                "use_external_sub": use_external_sub,
                "external_sub_map": external_sub_map or {},
                "max_workers": max_workers,
            },
        )

    @mcp.tool(annotations=annotations(open_world=True))
    async def torrent_status() -> dict[str, Any]:
        """Probe the shared active BitComet connection and its save folders.

        Includes actionable dependency/connection errors. Probing can pair a
        BitComet device; it does not start downloads. save_folders belong to the
        BitComet host, which may be remote.
        """
        return await api.request("GET", "/torrent/status")

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def torrent_devices() -> dict[str, Any]:
        """List saved BitComet devices and the shared active device ID.

        Passwords are represented only by has_password.
        """
        return await api.request("GET", "/torrent/devices")

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def torrent_device_add(
        label: str,
        url: str,
        username: str,
        password: str,
    ) -> dict[str, Any]:
        """Persist a remote BitComet's credentials and switch the backend to it.

        This affects web and AI callers and may drop in-flight metadata watches.
        Re-adding a saved URL updates its existing credentials/label. Credentials
        are stored by the existing backend device book.
        """
        return await api.request(
            "POST",
            "/torrent/devices",
            json={
                "label": label,
                "url": url,
                "username": username,
                "password": password,
            },
        )

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def torrent_device_update(
        device_id: str,
        label: str = "",
        url: str = "",
        username: str = "",
        password: str = "",
    ) -> dict[str, Any]:
        """Update a saved BitComet; blank fields retain their stored values.

        Editing the active connection may reset in-flight metadata watches.
        Passwords are saved on the backend and not returned.
        """
        return await api.request(
            "PATCH",
            f"/torrent/devices/{api.segment(device_id)}",
            json={
                "label": label,
                "url": url,
                "username": username,
                "password": password,
            },
        )

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def torrent_device_delete(device_id: str) -> dict[str, Any]:
        """Delete a saved remote device; may switch active device/reset watches.

        The backend's built-in local device cannot be removed.
        """
        return await api.request("DELETE", f"/torrent/devices/{api.segment(device_id)}")

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def torrent_device_select(device_id: str) -> dict[str, Any]:
        """Switch the shared backend BitComet device, resetting metadata watches.

        This changes the active device for the webpage as well.
        """
        return await api.request(
            "POST", f"/torrent/devices/{api.segment(device_id)}/select"
        )

    @mcp.tool(annotations=annotations(open_world=True))
    async def torrent_device_test(
        url: str,
        username: str = "",
        password: str = "",
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """Probe BitComet credentials and return save folders without saving them.

        A blank password plus device_id uses its saved password. This may create
        the backend's persisted pairing ID and pair with BitComet. Enable both
        Mobile App and Web UI switches under BitComet Remote Access.
        """
        return await api.request(
            "POST",
            "/torrent/devices/test",
            json={
                "url": url,
                "username": username,
                "password": password,
                "id": device_id,
            },
        )

    @mcp.tool(annotations=annotations(open_world=True))
    async def torrent_resolve(
        magnet: str | None = None,
        torrent_file: str | None = None,
        save_dir: str = "",
    ) -> dict[str, Any]:
        """Stage exactly one magnet or local .torrent path/file URI in BitComet.

        torrent_file is on the MCP host and is uploaded; save_dir is on the
        active BitComet host (blank uses backend default). Uploaded torrents are
        added stopped; new magnets run to fetch metadata and may download data
        until polled and deselected. Returns infohash and files/ready state.
        Poll torrent_resolve_status, then explicitly select returned file indices
        with torrent_send; discard a staged task with torrent_discard.
        """
        if bool(magnet) == bool(torrent_file):
            raise ToolError("Provide exactly one of magnet or torrent_file.")
        data = {"save_dir": save_dir}
        if magnet:
            data["magnet"] = magnet
        return await api.upload(
            "/torrent/resolve",
            [torrent_file] if torrent_file else [],
            data=data,
            field="file",
        )

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def torrent_resolve_status(infohash: str) -> dict[str, Any]:
        """Poll staged torrent metadata by infohash; returns file indices/ready.

        This is not read-only: first ready poll deselects files for review; an
        expired owned watch can discard its staged torrent. Use torrent_send
        once selection is complete. This does not monitor sent downloads.
        """
        return await api.request("GET", f"/torrent/resolve/{api.segment(infohash)}")

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def torrent_send(infohash: str, selected: list[int]) -> dict[str, Any]:
        """Apply explicit file indices and start the staged download in BitComet.

        Use indices returned by torrent_resolve/status, not array positions.
        After handover Toolkit forgets the watch: manage progress/cancellation
        and downloaded files in BitComet, not job_status or job_cancel.
        """
        return await api.request(
            "POST", "/torrent", json={"infohash": infohash, "selected": selected}
        )

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def torrent_discard(infohash: str) -> dict[str, Any]:
        """Discard an owned staged torrent before handover; keep downloaded files.

        This is not cancellation of a task already handed to BitComet.
        """
        return await api.request("DELETE", f"/torrent/{api.segment(infohash)}")

    @mcp.tool(annotations=annotations(open_world=True))
    async def webpdf_open(url: str) -> dict[str, Any]:
        """Open the backend's one shared Chrome session at a URL.

        Chrome/driver must be available on the backend. The user may need to
        scroll/load images in that Chrome window before webpdf_capture. A second
        session is rejected; webpage and AI share this session.
        """
        return await api.request("POST", "/webpdf/open", json={"url": url})

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def webpdf_status() -> dict[str, Any]:
        """Report whether the backend's shared Web Images Chrome session is open."""
        return await api.request("GET", "/webpdf/status")

    @mcp.tool(annotations=annotations(open_world=True))
    async def webpdf_scroll(
        steps: Annotated[int, Field(ge=1, le=20)] = 5,
        wait_ms: Annotated[int, Field(ge=0, le=2000)] = 500,
    ) -> dict[str, Any]:
        """Scroll the existing shared Chrome page down to load more images.

        Each step advances one viewport then waits wait_ms. Returns matched and
        loaded image counts, at_bottom and URL. Repeat until counts stabilize;
        lazy or virtual pages may still require manual interaction. Does not
        guarantee every image loaded. Then use webpdf_capture. Synchronous,
        bounded to 20 steps; does not create a job or a separate browser.
        """
        return await api.request(
            "POST", "/webpdf/scroll", json={"steps": steps, "wait_ms": wait_ms}
        )

    @mcp.tool(annotations=annotations(destructive=True, idempotent=True))
    async def webpdf_close() -> dict[str, Any]:
        """Close the shared Chrome capture session, losing its loaded page state."""
        return await api.request("POST", "/webpdf/close")

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def webpdf_capture() -> dict[str, Any]:
        """Synchronously capture loaded page images to PDF, then close Chrome.

        Existing backend selector is img[class*=bi]; this is not arbitrary page
        printing. Returns artifact_id/name/pages/skipped/warn; use
        artifact_download for the PDF. Failure may leave Chrome open. There is
        no backend job or reliable cancellation for this synchronous operation;
        on timeout check webpdf_status before deciding what to do next.
        """
        return await api.request("POST", "/webpdf/capture")

    @mcp.tool(annotations=annotations(destructive=True))
    async def gather_start(
        source: str,
        target: str,
        categories: list[str] | None = None,
        custom: str = "",
    ) -> dict[str, Any]:
        """Move matching files recursively into target; return job_id.

        source and target are absolute backend-host folders; target must be
        outside source. categories uses backend preset names (Video, Audio,
        Image, Document, Archive, Subtitle); custom is whitespace/comma-separated
        extensions or globs. Existing target names are auto-numbered. Scanning
        and moving occur in this same job, without a preview step. job_cancel
        stops between moves and never restores already moved files. Poll
        job_status for moved/failed names, scan errors, and target folder.
        """
        return await api.request(
            "POST",
            "/gather/start",
            json={
                "source": source,
                "target": target,
                "categories": categories or [],
                "custom": custom,
            },
        )

    @mcp.tool(annotations=annotations(destructive=True))
    async def image_to_pdf(
        files: list[str],
        output_path: str,
        name: str = "images.pdf",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Upload MCP-host image paths/file URIs; save one PDF locally.

        Pages use natural filename order (2 before 10), not supplied list order.
        Supports backend image formats including HEIC. output_path must name an
        absolute MCP-host destination file; parent directory must already exist.
        Existing output is refused unless overwrite=true. Returns path, byte
        count, media type and digest, never PDF bytes. Synchronous: no job ID or
        backend cancellation; a timeout does not mean conversion stopped.
        """
        return await api.upload(
            "/img-to-pdf",
            files,
            data={"name": name},
            output_path=output_path,
            overwrite=overwrite,
        )

    @mcp.tool(annotations=annotations())
    async def doc_to_pdf_start(files: list[str]) -> dict[str, Any]:
        """Upload MCP-host .docx paths/file URIs; start conversion, return job_id.

        Backend LibreOffice accepts tracked changes and removes comments in
        conversion copies; uploaded originals are unchanged. job_status returns
        done/failed files and a ZIP artifact_id for artifact_download.
        job_cancel stops conversion cooperatively; partial outputs may survive.
        """
        return await api.upload("/doc-to-pdf", files)

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def doc_to_markdown_health() -> dict[str, Any]:
        """Check backend MinerU executable and ONNX runtime availability.

        Readiness does not guarantee models are downloaded or conversion works.
        """
        return await api.request("GET", "/doc-to-markdown/health")

    @mcp.tool(annotations=annotations(open_world=True))
    async def doc_to_markdown_start(
        files: list[str],
        tier: Literal["flash", "basic", "standard", "advanced"] = "standard",
        ocr_mode: Literal["auto", "txt", "ocr"] = "auto",
        image_analysis: bool = True,
    ) -> dict[str, Any]:
        """Upload local paths/file URIs for MinerU conversion; return job_id.

        Accepts backend-supported PDFs, Office documents and images. Office uses
        native Flash parsing; PDF/image tier defaults to Standard. Models may
        download on the first run. Poll job_status for done/failed and artifact_id;
        use artifact_markdown to read actual Markdown text, or artifact_download
        for ZIP/images. job_cancel is cooperative and may leave partial results.
        """
        return await api.upload(
            "/doc-to-markdown",
            files,
            data={"tier": tier, "ocr_mode": ocr_mode, "image_analysis": image_analysis},
        )

    @mcp.tool(annotations=annotations())
    async def purge_scan(folder: str, patterns_raw: str) -> dict[str, Any]:
        """Preview cache/junk matches in an absolute backend-host folder.

        patterns_raw contains whitespace/comma-separated extensions or globs.
        Returns exact files, errors, total_bytes, rejected_tokens and scan_id.
        Stores a backend scan record (single use; expires after 30 minutes).
        Review the entire list before purge_delete; it deletes that whole list,
        not a caller-supplied subset. Refine patterns and rescan to change it.
        """
        return await api.request(
            "POST",
            "/purge/scan",
            json={"folder": folder, "patterns_raw": patterns_raw},
        )

    @mcp.tool(annotations=annotations(destructive=True))
    async def purge_delete(scan_id: str) -> dict[str, Any]:
        """Permanently delete every file from a reviewed scan_id; return job_id.

        Consumes the backend's single-use scan; expired/used IDs require rescan.
        There is no Trash or undo. job_cancel stops between batches of up to 16
        deletions and does not restore files. Poll job_status for deleted paths
        and failures.
        """
        return await api.request("POST", "/purge/delete", json={"scan_id": scan_id})

    @mcp.tool(annotations=annotations())
    async def photos_dry_run(
        source: str,
        dest: str,
        rules: str | None = None,
    ) -> dict[str, Any]:
        """Start a Photos library plan/verification job; return job_id.

        Absolute backend-host .photoslibrary paths are required. rules=null uses
        backend defaults; an empty string excludes nothing. Does not write the
        destination, but registers a job. Poll job_status for the plan/summary;
        job_cancel stops cooperatively. Review before photos_run.
        """
        return await api.request(
            "POST",
            "/photofilter/dry-run",
            json={"source": source, "dest": dest, "rules": rules},
        )

    @mcp.tool(annotations=annotations(destructive=True))
    async def photos_run(
        source: str,
        dest: str,
        rules: str | None = None,
    ) -> dict[str, Any]:
        """Mirror a Photos library without excluded caches; return job_id.

        Use the reviewed photos_dry_run paths/rules. Writes/overwrites dest and
        deletes destination entries absent from the plan; source is preserved.
        rules=null uses defaults, empty excludes nothing. job_cancel is
        cooperative and cannot undo already copied/deleted files. Poll
        job_status for verification summary and backend-host destination.
        """
        return await api.request(
            "POST",
            "/photofilter/run",
            json={"source": source, "dest": dest, "rules": rules},
        )

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def dependencies_scan(folder: str) -> dict[str, Any]:
        """Scan uv/npm manifests for upgrades in a backend folder; return job_id.

        This scan refreshes/writes lockfiles and contacts package registries; it
        is not read-only. Poll job_status to review bumps before applying.
        job_cancel is cooperative and does not roll back lockfile updates.
        Requires backend uv/npm and registry access for corresponding projects.
        """
        return await api.request("POST", "/deps/scan", json={"folder": folder})

    @mcp.tool(annotations=annotations(destructive=True, open_world=True))
    async def dependencies_apply_start(
        folder: str,
        commit: bool = False,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Start applying dependency updates to reviewed manifests; return job_id.

        Writes manifests/locks throughout folder. commit=true additionally makes
        Git commits; false (MCP default) leaves changes uncommitted. message is
        the optional Git subject. Uses current backend manifest/lock state, not
        a scan snapshot or selected subset. Poll job_status for per-manifest
        changes and commit SHAs. job_cancel only stops between manifest writes
        and repository commits; completed writes/commits are not rolled back.
        """
        return await api.request(
            "POST",
            "/deps/apply/start",
            json={"folder": folder, "commit": commit, "message": message},
        )

    @mcp.tool(annotations=annotations())
    async def subscription_generate(
        node_links: str,
        preferred_ips: str,
        name_prefix: str = "",
        keep_original_host: bool = True,
    ) -> dict[str, Any]:
        """Generate/store an optimized-IP subscription; returns sub_id and URLs.

        node_links contains newline-separated vmess/vless/trojan links or a
        base64 subscription. preferred_ips contains IP/host[:port][#remark]
        entries, separated by newlines, commas or semicolons.
        Backend expands and deduplicates nodes. Stored configuration and returned
        URLs may contain secrets/access tokens; keep them private. Use
        subscription_render to read actual config, or subscription_download.
        """
        return await api.request(
            "POST",
            "/subs/generate",
            json={
                "node_links": node_links,
                "preferred_ips": preferred_ips,
                "name_prefix": name_prefix,
                "keep_original_host": keep_original_host,
            },
        )

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def subscription_history() -> dict[str, Any]:
        """List persisted subscriptions with IDs, node counts and creation dates."""
        return {"subscriptions": await api.request("GET", "/subs/history")}

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def subscription_get(sub_id: str) -> dict[str, Any]:
        """Read a stored subscription's node summary, counts, and download URLs."""
        return await api.request("GET", f"/subs/{api.segment(sub_id)}")

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def subscription_urls(sub_id: str) -> dict[str, Any]:
        """Build public client subscription URLs; does not verify ID existence.

        Includes auto, raw, Clash and Surge; URLs may include access tokens and
        require the backend's advertised hostname/port to be reachable.
        """
        return await api.request("GET", f"/subs/{api.segment(sub_id)}/urls")

    @mcp.tool(annotations=annotations(read_only=True, idempotent=True))
    async def subscription_render(
        sub_id: str,
        target: Literal["raw", "clash", "surge"] = "raw",
    ) -> dict[str, Any]:
        """Return actual subscription configuration text for a target client.

        Config contains node credentials. Use subscription_download to save a
        complete config outside model context when it is large.
        """
        return await api.request(
            "GET", f"/subs/{api.segment(sub_id)}/render", params={"target": target}
        )

    @mcp.tool(annotations=annotations(destructive=True))
    async def subscription_download(
        sub_id: str,
        output_path: str,
        target: Literal["raw", "clash", "surge"] = "raw",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Save subscription config to an absolute MCP-host file; return metadata.

        Existing files are refused unless overwrite=true; parent must exist.
        Saved content contains credentials. No binary data enters model context.
        """
        return await api.download(
            f"/subs/{api.segment(sub_id)}/render",
            output_path,
            overwrite=overwrite,
            params={"target": target},
        )

    @mcp.tool(annotations=annotations(destructive=True))
    async def subscription_qr_download(
        sub_id: str,
        output_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Save the raw-subscription URL QR PNG to an absolute MCP-host path.

        Existing files are refused unless overwrite=true; parent must exist.
        QR may encode a private access token; ID existence is not checked by this
        backend route. Use subscription_get first when uncertain.
        """
        return await api.download(
            f"/subs/{api.segment(sub_id)}/qr.png", output_path, overwrite=overwrite
        )

    @mcp.tool(annotations=annotations(destructive=True, idempotent=True))
    async def subscription_delete(sub_id: str) -> dict[str, Any]:
        """Permanently delete a saved subscription, invalidating its client URLs."""
        return await api.request("DELETE", f"/subs/{api.segment(sub_id)}")
