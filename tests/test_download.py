from youtube2podcast.download import DownloadError, YtDlpDownloader

VTT = """WEBVTT

00:00:01.000 --> 00:00:02.000
Hello there.
"""


def test_probe_uses_next_english_track_when_one_times_out(tmp_path):
    class Downloader(YtDlpDownloader):
        def _extract(self, url, workdir, extra, download=True):
            if download is False:
                return {
                    "id": "abc",
                    "title": "Talk",
                    "subtitles": {"en": [{"url": "a"}], "en-orig": [{"url": "b"}]},
                    "automatic_captions": {},
                }
            langs = extra.get("subtitleslangs") or []
            if not langs:
                return {"id": "abc"}
            if langs == ["en"]:
                raise DownloadError("下载失败：\x1b[0;31mERROR:\x1b[0m The read operation timed out")
            if langs == ["en-orig"]:
                (workdir / "abc.en-orig.vtt").write_text(VTT, encoding="utf-8")
                return {"id": "abc"}
            raise AssertionError(extra)

    media = Downloader().probe("https://www.youtube.com/watch?v=abc", tmp_path)
    assert media.cues is not None
    assert media.cues[0].text == "Hello there."
