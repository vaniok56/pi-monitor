from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import host_info  # noqa: E402


class HostInfoLabelTests(unittest.TestCase):
    def test_detect_prefers_docker_daemon_hostname_when_env_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "host_info._detect_host_class", return_value="debian_arm64"
        ), patch("host_info._probe_capabilities", return_value={}), patch(
            "host_info.socket.gethostname", return_value="container-id"
        ), patch(
            "host_info.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="raspik4b\n"),
        ):
            info = host_info.detect()

        self.assertEqual(info.host_label, "raspik4b")


if __name__ == "__main__":
    unittest.main()
