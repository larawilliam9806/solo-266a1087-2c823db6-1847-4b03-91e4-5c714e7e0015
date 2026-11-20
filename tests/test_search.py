import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricing import create_app  # noqa: E402

# Box 1 "白砂" and box 2 "糖" share cell (0,0); box 7 straddles a column
# line; box 8 sits outside the grid.
SAMPLE = """框号,文字,x1,y1,x2,y2
1,白砂,10,10,50,40
2,糖,50,10,90,40
3,一级,110,10,190,40
4,5.00,210,10,290,40
5,红糖,10,110,90,140
6,1.20,210,110,290,140
7,跨格,90,10,110,40
8,出界,400,400,450,430
"""

H = [0, 100, 200]
V = [0, 100, 200, 300]


class SearchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.sqlite3")
        self.app = create_app({
            "TESTING": True,
            "DATABASE": self.db_path,
            "SECRET_KEY": "test",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, body=SAMPLE, filename="page.csv"):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(body.encode("utf-8")), filename)},
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    def upload_and_get_job(self):
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        return int(response.headers["Location"].rstrip("/").split("/")[-1])

    def set_lines(self, job_id, horizontal=H, vertical=V):
        return self.client.post(
            f"/api/jobs/{job_id}/lines",
            json={"horizontal": horizontal, "vertical": vertical},
        )

    def prepare(self):
        """A job with valid lines; returns its id."""
        job_id = self.upload_and_get_job()
        self.assertEqual(self.set_lines(job_id).status_code, 200)
        return job_id

    def search(self, query):
        response = self.client.get(f"/api/search?q={query}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["hits"]

    def box_id(self, job_id, ref):
        state = self.client.get(f"/api/jobs/{job_id}").get_json()
        return next(item["box_id"] for item in state["boxes"]
                    if item["box_ref"] == ref)

    def decide(self, job_id, box, kind, target_cell=None):
        payload = {"box_id": box, "kind": kind}
        if target_cell is not None:
            payload["target_cell"] = target_cell
        return self.client.post(f"/api/jobs/{job_id}/decisions", json=payload)


class SearchMatchTests(SearchTestCase):
    def test_query_may_span_boxes_within_one_cell(self):
        self.prepare()
        hits = self.search("白砂糖")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit["kind"], "cell")
        self.assertEqual((hit["row"], hit["col"], hit["column"]), (0, 0, "name"))
        self.assertEqual(hit["column_label"], "品名")
        self.assertEqual(len(hit["box_ids"]), 2)  # both boxes take part
        self.assertEqual(hit["match"], "白砂糖")

    def test_match_inside_a_single_placed_box(self):
        self.prepare()
        hits = self.search("白砂")
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(hits[0]["box_ids"]), 1)

    def test_query_never_spans_cells(self):
        self.prepare()
        # "糖" ends cell (0,0) and "一" starts cell (0,1): no cell holds both.
        self.assertEqual(self.search("糖一"), [])

    def test_pending_box_matches_as_single_box(self):
        self.prepare()
        hits = self.search("跨格")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit["kind"], "box")
        self.assertEqual(hit["status"], "pending")
        self.assertEqual(hit["status_label"], "待核")
        self.assertIsNone(hit["row"])

    def test_excluded_box_matches_as_single_box(self):
        job_id = self.prepare()
        box = self.box_id(job_id, "7")
        self.assertEqual(self.decide(job_id, box, "exclude").status_code, 200)
        hits = self.search("跨格")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["status"], "excluded")
        self.assertEqual(hits[0]["status_label"], "已排除")

    def test_stale_box_matches_as_single_box(self):
        job_id = self.prepare()
        box = self.box_id(job_id, "7")
        self.assertEqual(
            self.decide(job_id, box, "cell", "100|0|200|100").status_code, 200)
        # Moving a bordering line invalidates the decision -> 待重新确认.
        self.assertEqual(self.set_lines(job_id, H, [0, 95, 200, 300]).status_code, 200)
        hits = self.search("跨格")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "box")
        self.assertEqual(hits[0]["status"], "stale")
        self.assertEqual(hits[0]["status_label"], "待重新确认")

    def test_every_occurrence_is_listed(self):
        job_id = self.upload_and_get_job()
        self.assertEqual(self.set_lines(job_id).status_code, 200)
        # "糖" appears in cell (0,0) and in cell (1,0) ("红糖").
        hits = self.search("糖")
        self.assertEqual(len(hits), 2)
        self.assertEqual({(hit["row"], hit["col"]) for hit in hits},
                         {(0, 0), (1, 0)})

    def test_context_around_match(self):
        self.prepare()
        hit = self.search("砂")[0]
        self.assertEqual(hit["before"], "白")
        self.assertEqual(hit["match"], "砂")
        self.assertEqual(hit["after"], "糖")

    def test_empty_query_is_rejected(self):
        self.prepare()
        response = self.client.get("/api/search")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        response = self.client.get("/api/search?q=%20%20")
        self.assertEqual(response.status_code, 400)


class IndexSyncTests(SearchTestCase):
    def test_index_follows_manual_assignment(self):
        job_id = self.prepare()
        box = self.box_id(job_id, "7")
        self.assertEqual(
            self.decide(job_id, box, "cell", "100|0|200|100").status_code, 200)
        # The box joined cell (0,1): the cross-box query now matches there...
        hits = self.search("跨格一")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "cell")
        self.assertEqual((hits[0]["row"], hits[0]["col"]), (0, 1))
        # ...and the old single-box entry is gone.
        self.assertEqual([h for h in self.search("跨格") if h["kind"] == "box"], [])

    def test_index_follows_line_change(self):
        job_id = self.prepare()
        before = self.search("白砂糖")
        self.assertEqual(len(before), 1)
        # A new row line reshapes the cells; the old cell key must disappear.
        self.assertEqual(
            self.set_lines(job_id, [0, 50, 100, 200]).status_code, 200)
        after = self.search("白砂糖")
        self.assertEqual(len(after), 1)
        self.assertNotEqual(after[0]["entry_key"], before[0]["entry_key"])

    def test_index_follows_cleared_decision(self):
        job_id = self.prepare()
        box = self.box_id(job_id, "7")
        self.decide(job_id, box, "cell", "100|0|200|100")
        self.assertEqual(len(self.search("跨格一")), 1)
        response = self.client.delete(f"/api/jobs/{job_id}/decisions/{box}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.search("跨格一"), [])
        self.assertEqual(len(self.search("跨格")), 1)  # single-box entry back

    def test_deleted_job_drops_out_of_results(self):
        first = self.prepare()
        second = self.upload_and_get_job()
        self.assertEqual(self.set_lines(second).status_code, 200)
        self.assertEqual(len(self.search("白砂糖")), 2)
        response = self.client.post(f"/jobs/{first}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        hits = self.search("白砂糖")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["job_id"], second)

    def test_failed_line_update_keeps_task_and_old_hits(self):
        job_id = self.prepare()
        before = self.search("白砂糖")
        with mock.patch("pricing.db.rebuild_search_index",
                        side_effect=RuntimeError("disk full")):
            response = self.set_lines(job_id, [0, 100, 260])
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.get_json()["ok"])
        # The task kept its previous layout...
        state = self.client.get(f"/api/jobs/{job_id}").get_json()
        self.assertEqual(state["horizontal"], H)
        # ...and the old hits still come from the untouched index.
        self.assertEqual(self.search("白砂糖"), before)

    def test_failed_decision_update_keeps_task_and_old_hits(self):
        job_id = self.prepare()
        box = self.box_id(job_id, "7")
        before = self.search("跨格")
        with mock.patch("pricing.db.rebuild_search_index",
                        side_effect=RuntimeError("disk full")):
            response = self.decide(job_id, box, "exclude")
        self.assertEqual(response.status_code, 500)
        state = self.client.get(f"/api/jobs/{job_id}").get_json()
        item = next(entry for entry in state["boxes"] if entry["box_id"] == box)
        self.assertEqual(item["status"], "pending")  # decision was rolled back
        self.assertEqual(self.search("跨格"), before)

    def test_failed_clear_keeps_task_and_old_hits(self):
        job_id = self.prepare()
        box = self.box_id(job_id, "7")
        self.decide(job_id, box, "exclude")
        before = self.search("跨格")
        self.assertEqual(before[0]["status"], "excluded")
        with mock.patch("pricing.db.rebuild_search_index",
                        side_effect=RuntimeError("disk full")):
            response = self.client.delete(f"/api/jobs/{job_id}/decisions/{box}")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.search("跨格"), before)

    def test_failed_upload_leaves_no_task_behind(self):
        with mock.patch("pricing.db.rebuild_search_index",
                        side_effect=RuntimeError("disk full")):
            response = self.upload()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.search("白砂糖"), [])
        jobs = self.client.get("/").data.decode("utf-8")
        self.assertNotIn("page.csv", jobs)

    def test_invalid_lines_are_rejected_without_touching_index(self):
        job_id = self.prepare()
        before = self.search("白砂糖")
        response = self.set_lines(job_id, [0, 100], [0, 100, 200])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.search("白砂糖"), before)


class JobHitsTests(SearchTestCase):
    def test_hits_api_lists_current_hits_of_one_job(self):
        job_id = self.prepare()
        response = self.client.get(f"/api/jobs/{job_id}/hits?q=白砂糖")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total"], 1)
        hit = data["hits"][0]
        self.assertEqual(hit["job_id"], job_id)
        self.assertEqual(len(hit["box_ids"]), 2)

    def test_hit_disappears_after_task_change(self):
        job_id = self.prepare()
        hit = self.client.get(f"/api/jobs/{job_id}/hits?q=白砂糖").get_json()["hits"][0]
        # Reshape the grid so the recorded hit (entry + offset) is stale.
        self.assertEqual(
            self.set_lines(job_id, [0, 50, 100, 200]).status_code, 200)
        hits = self.client.get(f"/api/jobs/{job_id}/hits?q=白砂糖").get_json()["hits"]
        self.assertFalse(any(
            entry["entry_key"] == hit["entry_key"] and entry["offset"] == hit["offset"]
            for entry in hits))

    def test_hits_api_requires_query_and_job(self):
        job_id = self.prepare()
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}/hits").status_code, 400)
        self.assertEqual(self.client.get("/api/jobs/99999/hits?q=糖").status_code, 404)


class SearchPageTests(SearchTestCase):
    def test_search_page_lists_hits_with_links(self):
        job_id = self.prepare()
        response = self.client.get("/search?q=白砂糖")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn("<mark>白砂糖</mark>", html)
        self.assertIn("第 1 行 · 品名", html)
        self.assertIn(f"/jobs/{job_id}?q=", html)
        self.assertIn("entry=", html)
        self.assertIn("offset=", html)

    def test_search_page_marks_unplaced_boxes(self):
        self.prepare()
        html = self.client.get("/search?q=跨格").data.decode("utf-8")
        self.assertIn("未归位 · 待核", html)

    def test_search_page_without_query_and_without_results(self):
        self.assertEqual(self.client.get("/search").status_code, 200)
        self.prepare()
        html = self.client.get("/search?q=不存在").data.decode("utf-8")
        self.assertIn("没有找到", html)

    def test_index_page_offers_search_form(self):
        html = self.client.get("/").data.decode("utf-8")
        self.assertIn('action="/search"', html)


class ExistingJobTests(SearchTestCase):
    def test_jobs_saved_before_the_index_are_searchable_after_restart(self):
        job_id = self.prepare()
        self.assertEqual(len(self.search("白砂糖")), 1)
        # Simulate a database written before the index existed.
        connection = sqlite3.connect(self.db_path)
        connection.execute("DELETE FROM search_entries")
        connection.commit()
        connection.close()
        self.assertEqual(self.search("白砂糖"), [])
        # Reopening the app backfills the index for the existing job.
        restarted = create_app({
            "TESTING": True,
            "DATABASE": self.db_path,
            "SECRET_KEY": "test",
        })
        client = restarted.test_client()
        hits = client.get("/api/search?q=白砂糖").get_json()["hits"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["job_id"], job_id)


if __name__ == "__main__":
    unittest.main()
