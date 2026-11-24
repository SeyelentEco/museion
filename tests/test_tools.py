"""Installing the helper programs, from archives made on the spot and served
by a local server: downloaded, checked, unpacked and flattened, with the
companion archive (the CUDA runtime) unpacked over its build."""

import hashlib
import io
import os
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fake_http import FakeServer
from museion import catalog, hardware, tools

EXE = ".exe" if sys.platform == "win32" else ""


def tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if "." not in name.rsplit("/", 1)[-1] else 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def zip_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | 0o755) << 16  # made on Unix, executable
            zf.writestr(info, data)
    return buf.getvalue()


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name, "bin")
        self.steps: list[tools.Step] = []
        self.archives = {
            "kiwix.tar.gz": tar_gz({f"kiwix-tools_1.0/kiwix-serve{EXE}": b"kiwix",
                                    "kiwix-tools_1.0/lib/libzim.so.9": b"zim",
                                    "kiwix-tools_1.0/README.md": b"readme"}),
            "llama.zip": zip_of({f"build/bin/llama-server{EXE}": b"llama", "build/bin/ggml.dll": b"ggml"}),
            "cudart.zip": zip_of({"cudart64_12.dll": b"cudart"}),
        }
        for name, data in self.archives.items():
            self.server.files["/" + name] = data
        self.kiwix = catalog.Tool("kiwix-tools", "kiwix-tools", ["kiwix-serve"], {"any": self.variant("kiwix.tar.gz")})
        self.llama = catalog.Tool("llama-cpp", "llama.cpp", ["llama-server"], {
            "any-cpu": self.variant("llama.zip", accelerator="cpu"),
            "any-cuda": self.variant("llama.zip", accelerator="cuda", also=["any-cudart"]),
            "any-cudart": self.variant("cudart.zip", internal=True),
        })
        patcher = mock.patch.object(catalog, "tools", lambda: [self.kiwix, self.llama])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def variant(self, file: str, **kw) -> catalog.ToolVariant:
        data = self.archives[file]
        return catalog.ToolVariant(file, f"{self.server.url}/{file}", len(data),
                                   hashlib.sha256(data).hexdigest(), **kw)

    def test_installs_unpacks_and_flattens(self):
        staging = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(*args, **kw):
            staging.append(real_mkdtemp(*args, **kw))
            return staging[-1]

        with mock.patch.object(tools.tempfile, "mkdtemp", mkdtemp):
            installed = tools.install_selected("cuda", self.dest, [], self.steps.append)

        self.assertEqual(installed.kiwix_serve, self.dest / f"kiwix-serve{EXE}")
        self.assertEqual(installed.llama_server, self.dest / f"llama-server{EXE}")
        self.assertEqual(installed.pdfium_dir, self.dest)
        # Programs and libraries come up to the top; the rest stays put.
        for name in ("libzim.so.9", "ggml.dll", "cudart64_12.dll"):
            self.assertTrue((self.dest / name).is_file(), name)
        self.assertTrue((self.dest / "kiwix-tools_1.0" / "README.md").is_file())
        if sys.platform != "win32":
            # zipfile drops permissions unless they are put back.
            self.assertTrue(os.access(installed.llama_server, os.X_OK))
            self.assertTrue(os.access(installed.kiwix_serve, os.X_OK))
        self.assertFalse(Path(staging[0]).exists(), "the downloaded archives are cleaned up")

        kinds = [(s.kind, s.tool, s.file) for s in self.steps if s.kind != tools.DOWNLOADING]
        self.assertEqual(kinds, [
            (tools.UNPACKING, "kiwix-tools", ""), (tools.DONE, "kiwix-tools", ""),
            (tools.UNPACKING, "llama.cpp", ""), (tools.UNPACKING, "llama.cpp", ""), (tools.DONE, "llama.cpp", ""),
        ])
        downloaded = {s.file for s in self.steps if s.kind == tools.DOWNLOADING}
        self.assertEqual(downloaded, {"kiwix.tar.gz", "llama.zip", "cudart.zip"})

    def test_the_accelerator_picks_the_build(self):
        tools.install(hardware.Accelerator(hardware.CPU), self.dest, self.steps.append)
        self.assertFalse((self.dest / "cudart64_12.dll").exists())
        self.assertTrue((self.dest / f"llama-server{EXE}").exists())

    def test_only_the_named_tools_are_installed(self):
        installed = tools.install_selected("cpu", self.dest, ["kiwix-tools"])
        self.assertIsNotNone(installed.kiwix_serve)
        self.assertIsNone(installed.llama_server)

    def test_a_tool_with_no_build_here_is_skipped(self):
        self.kiwix.variants = {"elsewhere": self.variant("kiwix.tar.gz", platform="plan9")}
        tools.install_selected("cpu", self.dest, ["kiwix-tools"], self.steps.append)
        self.assertEqual(self.steps, [tools.Step(tools.SKIPPED, "kiwix-tools", reason="no build for this platform")])

    def test_a_missing_companion_is_an_error(self):
        self.llama.variants["any-cuda"].also = ["nowhere"]
        with self.assertRaisesRegex(ValueError, "llama-cpp names missing companion nowhere"):
            tools.install_selected("cuda", self.dest, ["llama.cpp"])

    def test_an_archive_with_the_wrong_hash_is_never_unpacked(self):
        self.kiwix.variants["any"].sha256 = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            tools.install_selected("cpu", self.dest, ["kiwix-tools"], self.steps.append)
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertNotIn(tools.UNPACKING, [s.kind for s in self.steps])

    def test_the_plan_counts_the_companion(self):
        sizes = {name: (key, size) for name, key, size in tools.plan("cuda")}
        both = len(self.archives["llama.zip"]) + len(self.archives["cudart.zip"])
        self.assertEqual(sizes["llama.cpp"], ("any-cuda", both))
        self.assertEqual(sizes["kiwix-tools"], ("any", len(self.archives["kiwix.tar.gz"])))


class FlattenAndExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_file_already_at_the_top_is_not_replaced(self):
        (self.dir / "llama-server").write_text("kept")
        (self.dir / "build").mkdir()
        (self.dir / "build" / "llama-server").write_text("newer")
        tools.flatten(self.dir)
        self.assertEqual((self.dir / "llama-server").read_text(), "kept")
        self.assertTrue((self.dir / "build" / "llama-server").exists())

    def test_flatten_stops_four_folders_down(self):
        deep = self.dir.joinpath("a", "b", "c", "d", "e", "f")
        deep.mkdir(parents=True)
        (deep / "too-deep").write_text("x")
        (deep.parent.parent / "found").write_text("x")  # a/b/c/d
        tools.flatten(self.dir)
        self.assertTrue((self.dir / "found").exists())
        self.assertFalse((self.dir / "too-deep").exists())

    def test_links_in_a_tar_are_unpacked_as_copies(self):
        # exFAT, which a drive is formatted with, cannot hold a symbolic link.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            def link(name, to, kind=tarfile.SYMTYPE):
                info = tarfile.TarInfo(name)
                info.type, info.linkname = kind, to
                tf.addfile(info)
            # Links before what they point at, and a link to a link.
            link("b/libx.so", "libx.so.0")
            link("b/libx.so.0", "libx.so.0.3.0")
            link("b/hard", "b/libx.so.0.3.0", tarfile.LNKTYPE)
            link("b/dangling", "nowhere")
            info = tarfile.TarInfo("b/libx.so.0.3.0")
            info.size, info.mode = 3, 0o755
            tf.addfile(info, io.BytesIO(b"lib"))
        archive = self.dir / "linked.tar.gz"
        archive.write_bytes(buf.getvalue())
        out = self.dir / "out"
        tools.extract(archive, out)
        for name in ("libx.so", "libx.so.0", "libx.so.0.3.0", "hard"):
            self.assertEqual((out / "b" / name).read_bytes(), b"lib", name)
            self.assertFalse((out / "b" / name).is_symlink(), name)
        self.assertFalse((out / "b" / "dangling").exists())

    def test_a_tar_member_that_escapes_is_refused(self):
        archive = self.dir / "evil.tar.gz"
        archive.write_bytes(tar_gz({"../escaped": b"x"}))
        with self.assertRaisesRegex(RuntimeError, "could not unpack"):
            tools.extract(archive, self.dir / "out")
        self.assertFalse((self.dir / "escaped").exists())

    def test_without_tarfile_filters_escapes_are_still_refused(self):
        # Pythons before 3.11.4 have no extraction filter; tools.py checks itself.
        for name, link in (("../escaped", None), ("link", "../../outside")):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                info = tarfile.TarInfo(name)
                if link:
                    info.type, info.linkname = tarfile.SYMTYPE, link
                tf.addfile(info, io.BytesIO(b""))
            buf.seek(0)
            with tarfile.open(fileobj=buf) as tf, self.assertRaisesRegex(ValueError, "would land outside"):
                tools._check_tar(tf, self.dir / "out")

    def test_find_exe(self):
        self.assertIsNone(tools.find_exe(self.dir, "kiwix-serve"))
        (self.dir / f"kiwix-serve{EXE}").write_text("")
        self.assertEqual(tools.find_exe(self.dir, "kiwix-serve"), self.dir / f"kiwix-serve{EXE}")


if __name__ == "__main__":
    unittest.main()
