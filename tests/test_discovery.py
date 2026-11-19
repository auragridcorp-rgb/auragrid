"""Discovery files must match the live routes exactly (machine-discovery Phase 2)."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_plans_samples as t  # noqa: E402  (sets env, stubs providers, imports the app)
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

backend = t.backend
NOT_DATA = re.compile(r"^/api/v1/(price|sample|pro)/")


def live_data_paths() -> set:
    """GET routes under /api/v1/ that serve data (not retired, not quotes), plus plan paths."""
    retired = {"/api/v1/" + slug for slug in backend.RETIRED}
    paths = {r.path for r in backend.app.routes
             if isinstance(r, APIRoute) and "GET" in r.methods and r.path.startswith("/api/v1/")
             and not NOT_DATA.match(r.path) and r.path not in retired}
    paths |= {p for plan in backend.PLANS for p in plan.paths()}
    return paths


def paid_paths() -> set:
    return {"/api/v1/" + slug for slug, *_ in backend.PAID_CATALOG} | \
           {p for plan in backend.PLANS for p in plan.paths()}


class DiscoveryMatchesRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(backend.app)
        cls.live = live_data_paths()

    def test_openapi(self):
        spec = self.client.get("/openapi.json").json()
        documented = {p for p in spec["paths"] if p.startswith("/api/v1/") and not NOT_DATA.match(p)}
        self.assertEqual(documented, self.live)
        for path in paid_paths():
            op = spec["paths"][path]["get"]
            self.assertIn("402", op["responses"], path)
            self.assertIn("x-l402", op, path)

    def test_manifest(self):
        m = self.client.get("/agent-manifest.json").json()
        documented = {e["path"] for e in m["endpoints"]} | {p for plan in m["plans"] for p in plan["paths"]}
        self.assertEqual(documented, self.live)
        self.assertEqual(m["endpoints_count"], len(m["endpoints"]))
        for e in m["endpoints"]:
            for field in ("source", "license", "update_interval_seconds", "stale_after_seconds", "status"):
                self.assertIn(field, e, (e["path"], field))
            if e["access"] == "paid":
                self.assertIn("sample_url", e, e["path"])
                self.assertIn(e["sample_url"], self.live)

    def test_llms_txt(self):
        text = self.client.get("/llms.txt").text
        documented = set(re.findall(r"(/api/v1/[a-z0-9/-]+)", text))
        self.assertEqual(documented - set(re.findall(r"/api/v1/price/[a-z0-9/-]+", text)), self.live)

    def test_l402_services(self):
        doc = self.client.get("/.well-known/l402-services").json()
        documented = {s["url"].split("://", 1)[-1].split("/", 1)[-1] for s in doc["services"]}
        self.assertEqual({"/" + d for d in documented}, paid_paths())

    def test_status(self):
        st = self.client.get("/status.json").json()
        self.assertEqual({f["path"] for f in st["feeds"]}, {"/api/v1/" + slug for slug, *_ in backend.OFFERED})

    def test_every_documented_path_routes_somewhere(self):
        for path in self.live:
            r = self.client.get(path, headers={**t.GW, **t.L402})
            self.assertNotIn(r.status_code, (404, 405), path)

    def test_robots_allows_discovery(self):
        robots = self.client.get("/robots.txt").text
        for path in ("/llms.txt", "/agent-manifest.json", "/openapi.json", "/.well-known/", "/status"):
            self.assertIn("Allow: " + path, robots)
