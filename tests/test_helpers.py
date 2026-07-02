"""Unit tests for the pure helper functions inside the tool pages."""


def test_natural_sort_orders_numbers_humanly(remux):
    names = ["ep1", "ep10", "ep2", "Ep20", "ep3"]
    assert sorted(names, key=remux.natural_sort_key) == [
        "ep1",
        "ep2",
        "ep3",
        "ep10",
        "Ep20",
    ]


def test_natural_sort_is_consistent_between_tools(remux, gatherer):
    names = ["b2", "b10", "a1"]
    assert sorted(names, key=remux.natural_sort_key) == sorted(
        names, key=gatherer.natural_sort_key
    )


def test_applescript_str_escapes_quotes_and_backslashes(picker):
    assert picker._applescript_str("/a/b") == '"/a/b"'
    assert picker._applescript_str('a"b') == '"a\\"b"'
    assert picker._applescript_str("a\\b") == '"a\\\\b"'


def test_normalize_pattern(gatherer):
    assert gatherer.normalize_pattern("srt") == "*.srt"
    assert gatherer.normalize_pattern(".srt") == "*.srt"
    assert gatherer.normalize_pattern("*.mkv") == "*.mkv"
    assert gatherer.normalize_pattern("report*.pdf") == "report*.pdf"
    assert gatherer.normalize_pattern("   ") is None


def test_build_ffmpeg_cmd_copies_and_tags_subtitle(remux):
    cmd = remux.build_ffmpeg_cmd(
        "in.mkv",
        None,
        "out.mkv",
        {"video": 0, "audio": [0], "subtitle": 0},
        "chi",
    )
    assert cmd[0] == "ffmpeg"
    assert "copy" in cmd  # stream-copy, no re-encode
    joined = " ".join(cmd)
    assert "title=in" in joined
    assert "language=chi" in joined


def test_build_mineru_cmd_pipeline_includes_method_lang_and_toggles(markdown):
    cmd = markdown.build_mineru_cmd(
        ["mineru"],
        "in.pdf",
        "out",
        backend="pipeline",
        method="ocr",
        lang="ch",
        formula=True,
        table=False,
    )
    assert cmd[:7] == ["mineru", "-p", "in.pdf", "-o", "out", "-b", "pipeline"]
    joined = " ".join(cmd)
    assert "-m ocr" in joined
    assert "-l ch" in joined
    assert "-f true" in joined
    assert "-t false" in joined
    assert "--effort" not in joined


def test_build_mineru_cmd_hybrid_uses_effort_not_pipeline_flags(markdown):
    cmd = markdown.build_mineru_cmd(
        ["mineru"],
        "in.pdf",
        "out",
        backend="hybrid-engine",
        effort="high",
    )
    joined = " ".join(cmd)
    assert "--effort high" in joined
    assert "-m " not in joined
    assert "-l " not in joined


def test_build_ffmpeg_cmd_omits_subtitle_metadata_when_absent(remux):
    cmd = remux.build_ffmpeg_cmd(
        "in.mkv",
        None,
        "out.mkv",
        {"video": 0, "audio": [0], "subtitle": None},
        "chi",
    )
    joined = " ".join(cmd)
    assert "language=" not in joined
    assert "disposition" not in joined
