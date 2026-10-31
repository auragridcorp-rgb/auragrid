"""The packaged client (auragrid_mcp) must stay identical to the hardened root client.py."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ClientCopyTests(unittest.TestCase):
    def test_packaged_client_matches_root(self):
        with open(os.path.join(ROOT, "client.py")) as fh:
            root = fh.read()
        with open(os.path.join(ROOT, "auragrid_mcp", "auragrid_mcp", "client.py")) as fh:
            packaged = fh.read()
        self.assertEqual(packaged.split("\n", 2)[2], root)


if __name__ == "__main__":
    unittest.main()
