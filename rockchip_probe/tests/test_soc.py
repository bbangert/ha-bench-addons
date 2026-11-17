"""Host-side checks for the per-SoC table. Run: python3 -m unittest discover -s rockchip_probe/tests"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(APP, "probe"))

import rknn_fetch  # noqa: E402
import rockchip_probe as rp  # noqa: E402


def soc_for(compatible):
    rp.read_device_tree = lambda prop: ("/proc/device-tree", b"\0".join(c.encode() for c in compatible) + b"\0"
                                        if prop == "compatible" else b"Some Board\0")
    return rp.soc_info()


class SocTable(unittest.TestCase):
    def test_detection(self):
        for compatible, name in ((["radxa,rock-4d", "rockchip,rk3576"], "rk3576"),
                                 (["rockchip,rk3566"], "rk3566"),
                                 (["rockchip,rk3568"], "rk3566"),
                                 (["rockchip,rk3588"], None)):
            soc = soc_for(compatible)
            self.assertEqual(soc["name"], name, compatible)
            self.assertEqual(soc["supported"], name is not None)

    def test_unlisted_soc_falls_back_and_says_so(self):
        entry, guessed = rp.soc_choices(soc_for(["rockchip,rk3588"]))
        self.assertTrue(guessed)
        self.assertEqual(entry, rp.SOCS[0])
        self.assertFalse(rp.soc_choices(soc_for(["rockchip,rk3576"]))[1])

    def test_every_model_is_pinned_in_runtime_sources(self):
        sources = {e["name"]: e for e in rknn_fetch.read_sources(os.path.join(APP, "RUNTIME_SOURCES"))}
        for entry in rp.SOCS:
            self.assertEqual(sources[entry["model_entry"]]["file"], entry["model_file"])

    def test_only_the_socs_model_is_fetched(self):
        sources = rknn_fetch.read_sources(os.path.join(APP, "RUNTIME_SOURCES"))
        names = [e["name"] for e in rknn_fetch.select_sources(sources, "resnet18-model-rk3576")]
        self.assertIn("resnet18-model-rk3576", names)
        self.assertNotIn("resnet18-model-rk3566", names)
        for shared in ("librknnrt", "rknn-toolkit-lite2", "resnet18-image", "resnet18-labels"):
            self.assertIn(shared, names)


class NpuExpectations(unittest.TestCase):
    def test_top5_per_soc(self):
        by = {e["name"]: e["expected_top5"] for e in rp.SOCS}
        self.assertEqual(by["rk3566"], [812, 404, 657, 466, 895])
        self.assertEqual(by["rk3576"], [812, 404, 657, 833, 466])
        for top5 in by.values():
            self.assertEqual(top5[0], 812)


class Encoder(unittest.TestCase):
    LOG = "mpp[1]: found api hal_h264e_vepu510 for coding 7 client 3\nencode 30 frames\n"

    def check(self, hal):
        return rp.encode_check({"rc": 0, "stdout": self.LOG, "stderr": ""}, 1000, 30, hal)[0]

    def test_expected_hal(self):
        c = self.check("hal_h264e_vepu510")
        self.assertTrue(c["ok"] and c["hal_as_expected"])

    def test_other_hal_is_reported_not_failed(self):
        c = self.check("hal_h264e_vepu541")
        self.assertTrue(c["ok"])
        self.assertFalse(c["hal_as_expected"])

    def test_frame_count_still_gates(self):
        r = {"rc": 0, "stdout": self.LOG.replace("encode 30", "encode 29"), "stderr": ""}
        self.assertFalse(rp.encode_check(r, 1000, 30, "hal_h264e_vepu510")[0]["ok"])


class RgaHeaps(unittest.TestCase):
    def results(self, cma_detail):
        out = []
        for buf, ok, detail in (("system", True, ""), ("rk-dma-heap-cma", False, cma_detail)):
            out += [{"case": c, "buf": buf, "ok": ok, "detail": detail} for c in rp.RGA_CASES]
        out += [{"case": "version", "buf": "-", "ok": True, "detail": "v"},
                {"case": "stress", "buf": "system", "ok": True, "detail": "", "jobs": 500, "failures": 0,
                 "checksums": 1}]
        return out

    def test_missing_heap_is_not_present_and_does_not_gate(self):
        problems, summary = rp.rga_verdict(self.results(
            "open /dev/rk_dma_heap/rk-dma-heap-cma: No such file or directory"))
        self.assertEqual(problems, [])
        self.assertEqual(summary["rk-dma-heap-cma"], "not present")

    def test_missing_rga_device_is_a_failure_not_a_missing_heap(self):
        detail = "open /dev/rga: No such file or directory; librga not called"
        problems, summary = rp.rga_verdict(self.results(detail))
        self.assertTrue(summary["rk-dma-heap-cma"].startswith("failed"))
        self.assertFalse(rp.heap_absent({"ok": False, "detail": detail}))
        self.assertTrue(rp.heap_absent({"ok": False, "detail": "open /dev/dma_heap/reserved: No such file or directory"}))

    def test_a_refused_allocation_is_still_a_failure_for_that_heap(self):
        _, summary = rp.rga_verdict(self.results("DMA_HEAP_IOCTL_ALLOC on /dev/x: Cannot allocate memory"))
        self.assertTrue(summary["rk-dma-heap-cma"].startswith("failed"))


if __name__ == "__main__":
    unittest.main()
