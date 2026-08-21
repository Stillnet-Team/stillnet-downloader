import unittest

import bot


class YoutubeUrlTests(unittest.TestCase):
    def test_supported_urls_are_normalized(self) -> None:
        expected = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        urls = (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=10",
            "https://youtube.com/shorts/dQw4w9WgXcQ",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(bot.normalize_youtube_url(url), expected)

    def test_unsupported_urls_are_rejected(self) -> None:
        urls = (
            "not a url",
            "https://youtube.com.evil/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ&list=PL123",
            "https://youtube.com/channel/example",
            "https://youtu.be/too-short",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(bot.normalize_youtube_url(url))


class QualityTests(unittest.TestCase):
    def test_only_exact_available_video_qualities_are_shown(self) -> None:
        info = {
            "formats": [
                {"height": 360, "vcodec": "avc1", "acodec": "none", "filesize": 10_000_000},
                {"height": 720, "vcodec": "avc1", "acodec": "none", "filesize": 20_000_000},
                {"height": None, "vcodec": "none", "acodec": "mp4a", "filesize": 2_000_000},
            ]
        }
        self.assertEqual(bot.available_video_qualities(info), (360, 720))

    def test_oversize_formats_are_hidden(self) -> None:
        info = {
            "formats": [
                {"height": 1080, "vcodec": "avc1", "acodec": "none",
                 "filesize": bot.MAX_FILE_SIZE + 1},
                {"height": None, "vcodec": "none", "acodec": "mp4a", "filesize": 1},
            ]
        }
        self.assertEqual(bot.available_video_qualities(info), ())

    def test_audio_bitrate_is_limited_by_duration(self) -> None:
        self.assertEqual(bot.available_audio_bitrates({"duration": 3000}), (128,))


if __name__ == "__main__":
    unittest.main()
