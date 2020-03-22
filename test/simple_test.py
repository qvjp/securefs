#!/usr/bin/python3
# coding: utf-8
import os
import subprocess
import unittest
import tempfile
import shutil
import errno
import platform
import time
import traceback
import uuid
import sys
import stat
import traceback
import signal
import logging
import shlex
import ctypes
import faulthandler
from typing import *

faulthandler.enable()


def find_securefs_binary():
    for dir_path, _, files in os.walk("."):
        for fn in files:
            if fn == "securefs" or fn == "securefs.exe":
                return os.path.join(dir_path, fn)
    raise RuntimeError("securefs binary not found")


SECUREFS_BINARY = find_securefs_binary()

IS_WINDOWS = os.name == "nt"

if platform.system() == "Darwin":
    try:
        import xattr
    except ImportError:
        sys.stderr.write(
            'Importing module "xattr" failed. Testing for extended attribute support is skipped\n'
        )
        xattr = None
else:
    xattr = None

REFERENCE_DATA_DIR = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "reference"
)


class TimeoutException(BaseException):
    def __init__(self):
        BaseException.__init__(self, "Operation timeout")


if IS_WINDOWS:

    def ismount(path):
        # Not all reparse points are mounts, but in our test, that is close enough
        return (
            subprocess.call(
                ["fsutil", "reparsepoint", "query", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            == 0
        )


else:
    ismount = os.path.ismount


def securefs_mount(data_dir: str, mount_point: str, password: str) -> subprocess.Popen:
    command = [
        SECUREFS_BINARY,
        "mount",
        "--pass",
        password,
        "--log",
        "XXXX.log",
        "--trace",
        data_dir,
        mount_point,
    ]
    logging.info("Start mounting, command:\n%s", " ".join(command))
    p = subprocess.Popen(
        command, creationflags=subprocess.CREATE_NEW_CONSOLE if IS_WINDOWS else 0
    )

    for _ in range(100):
        time.sleep(0.05)
        try:
            if ismount(mount_point):
                return p
        except EnvironmentError:
            traceback.print_exc()
    raise TimeoutException()


def securefs_unmount(p: subprocess.Popen, mount_point: str):
    try:
        if IS_WINDOWS:
            ctrl_c_py = os.path.join(
                os.path.dirname(os.path.realpath(__file__)), "ctrl_c.py"
            )
            subprocess.check_call([sys.executable, ctrl_c_py, str(p.pid)])
            p.communicate(timeout=5)
        else:
            p.send_signal(signal.SIGINT)
            p.communicate(timeout=5)
        if p.returncode:
            raise RuntimeError(f"securefs failed with code {p.returncode}")
        if ismount(mount_point):
            raise RuntimeError(f"{mount_point} still mounted")
    except:
        if ismount(mount_point):
            raise  # Still mounted
        traceback.print_exc()


def securefs_create(data_dir, password, version):
    p = subprocess.Popen(
        [
            SECUREFS_BINARY,
            "create",
            "--format",
            str(version),
            data_dir,
            "--rounds",
            "4",
            "--pass",
            password,
        ],
    )
    p.communicate(timeout=5)


def make_test_case(format_version):
    class SimpleSecureFSTestBase(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            try:
                os.mkdir("tmp")
            except EnvironmentError as e:
                if e.errno != errno.EEXIST:
                    raise
            cls.data_dir = tempfile.mkdtemp(
                prefix="securefs.format{}.data_dir".format(format_version), dir="tmp"
            )
            cls.mount_point = tempfile.mkdtemp(
                prefix="securefs.format{}.mount_point".format(format_version),
                dir="tmp",
            )
            # On Windows it is not possible to mount to an existing directory
            os.rmdir(cls.mount_point)
            cls.password = "pvj8lRgrrsqzlr"
            securefs_create(cls.data_dir, cls.password, format_version)
            cls.mount()

        @classmethod
        def tearDownClass(cls):
            cls.unmount()

        @classmethod
        def mount(cls):
            cls.securefs_process = securefs_mount(
                cls.data_dir, cls.mount_point, cls.password
            )

        @classmethod
        def unmount(cls):
            if cls.securefs_process is None:
                return
            securefs_unmount(cls.securefs_process, cls.mount_point)
            cls.securefs_process = None

        def test_long_name(self):
            with self.assertRaises(EnvironmentError) as context:
                os.mkdir(os.path.join(self.mount_point, "k" * 256))
                self.fail("mkdir should fail")
            if not IS_WINDOWS:
                self.assertEqual(context.exception.errno, errno.ENAMETOOLONG)

        if xattr:

            def test_xattr(self):
                fn = os.path.join(self.mount_point, str(uuid.uuid4()))
                try:
                    with open(fn, "wt") as f:
                        f.write("hello\n")
                    x = xattr.xattr(fn)
                    x.set("abc", "def")
                    x.set("123", "456")
                    self.unmount()
                    self.mount()
                    self.assertEqual(x.get("abc"), "def")
                    self.assertEqual(set(x.list()), {"abc", "123"})
                    xattr.removexattr(fn, "abc")
                    self.assertEqual(set(x.list()), {"123"})
                finally:
                    try:
                        os.remove(fn)
                    except EnvironmentError:
                        pass

        if format_version < 4 and not IS_WINDOWS:

            def test_hardlink(self):
                data = os.urandom(16)
                source = os.path.join(self.mount_point, str(uuid.uuid4()))
                dest = os.path.join(self.mount_point, str(uuid.uuid4()))
                try:
                    with open(source, "wb") as f:
                        f.write(data)
                    os.link(source, dest)
                    source_stat = os.stat(source)
                    dest_stat = os.stat(dest)
                    self.assertEqual(source_stat.st_mode, dest_stat.st_mode)
                    self.assertEqual(source_stat.st_mtime, dest_stat.st_mtime)
                    self.assertEqual(source_stat.st_size, dest_stat.st_size)
                    self.assertEqual(source_stat.st_nlink, 2)
                    with open(dest, "rb") as f:
                        self.assertEqual(data, f.read())
                    # Moving hard links onto each other is a no-op
                    os.rename(dest, source)
                    self.assertTrue(os.path.isfile(dest) and os.path.isfile(source))
                finally:
                    try:
                        os.remove(source)
                    except EnvironmentError:
                        pass
                    try:
                        os.remove(dest)
                    except EnvironmentError:
                        pass

        if not IS_WINDOWS:

            def test_symlink(self):
                data = os.urandom(16)
                source = os.path.join(self.mount_point, str(uuid.uuid4()))
                dest = os.path.join(self.mount_point, str(uuid.uuid4()))
                try:
                    with open(source, "wb") as f:
                        f.write(data)
                    os.symlink(source, dest)
                    self.assertEqual(os.readlink(dest), source)
                    os.remove(source)
                    with self.assertRaises(EnvironmentError):
                        with open(dest, "rb") as f:
                            f.read()
                finally:
                    try:
                        os.remove(source)
                    except EnvironmentError:
                        pass
                    try:
                        os.remove(dest)
                    except EnvironmentError:
                        pass

        def test_rename(self):
            data = os.urandom(32)
            source = os.path.join(self.mount_point, str(uuid.uuid4()))
            dest = os.path.join(self.mount_point, str(uuid.uuid4()))
            try:
                with open(source, "wb") as f:
                    f.write(data)
                source_stat = os.stat(source)
                self.assertFalse(os.path.isfile(dest))
                os.rename(source, dest)
                self.assertFalse(os.path.isfile(source))
                self.assertTrue(os.path.isfile(dest))
                dest_stat = os.stat(dest)
                self.assertEqual(source_stat.st_ino, dest_stat.st_ino)
                self.assertEqual(source_stat.st_size, dest_stat.st_size)
            finally:
                try:
                    os.remove(source)
                except EnvironmentError:
                    pass
                try:
                    os.remove(dest)
                except EnvironmentError:
                    pass

        def test_rename_dir(self):
            a = str(uuid.uuid4())
            b = str(uuid.uuid4())
            c = str(uuid.uuid4())
            cwd = os.getcwd()
            os.chdir(self.mount_point)
            try:
                os.mkdir(a)
                os.mkdir(os.path.join(a, b))
                os.mkdir(c)
                os.rename(a, os.path.join(c, a))
                self.assertTrue(os.path.isdir(os.path.join(c, a, b)))
            finally:
                try:
                    shutil.rmtree(a)
                except EnvironmentError:
                    pass
                try:
                    shutil.rmtree(c)
                except EnvironmentError:
                    pass
                os.chdir(cwd)

        def test_read_write_mkdir_listdir_remove(self):
            dir_names = set(str(i) for i in range(3))
            random_data = os.urandom(11111)
            rng_filename = os.path.join(self.mount_point, "rng")
            with open(rng_filename, "wb") as f:
                f.write(random_data)
            self.unmount()

            self.mount()
            with open(rng_filename, "rb") as f:
                self.assertEqual(f.read(), random_data)
            data = b"\0" * len(random_data) + b"0"
            with open(rng_filename, "wb") as f:
                f.write(data)
            with open(rng_filename, "rb") as f:
                self.assertEqual(f.read(), data)
            os.remove(rng_filename)
            for n in dir_names:
                os.mkdir(os.path.join(self.mount_point, n))
            for n in dir_names:
                os.mkdir(os.path.join(self.mount_point, "0", n))
            for n in dir_names:
                os.mkdir(os.path.join(self.mount_point, "0", "1", n))
            self.unmount()

            self.mount()
            self.assertEqual(set(os.listdir(self.mount_point)), dir_names)
            self.assertEqual(
                set(os.listdir(os.path.join(self.mount_point, "0"))), dir_names
            )
            self.assertEqual(
                set(os.listdir(os.path.join(self.mount_point, "0", "1"))), dir_names
            )
            for dn in dir_names:
                try:
                    shutil.rmtree(os.path.join(self.mount_point, dn))
                except EnvironmentError:
                    pass

        if format_version == 3:

            def test_time(self):
                rand_dirname = os.path.join(self.mount_point, str(uuid.uuid4()))
                os.mkdir(rand_dirname)
                st = os.stat(rand_dirname)
                self.assertTrue(
                    st.st_atime == st.st_ctime and st.st_ctime == st.st_mtime
                )
                self.assertAlmostEqual(st.st_atime, time.time(), delta=10)
                rand_filename = os.path.join(rand_dirname, "abc")
                with open(rand_filename, "w") as f:
                    f.write("1")
                os.utime(rand_filename, (1000.0, 1000.0))
                st = os.stat(rand_filename)
                self.assertEqual(st.st_mtime, 1000)
                with open(rand_filename, "w") as f:
                    f.write("1")
                st = os.stat(rand_filename)
                self.assertAlmostEqual(st.st_ctime, time.time(), delta=10)

    return SimpleSecureFSTestBase


class TestVersion1(make_test_case(1)):
    pass


class TestVersion2(make_test_case(2)):
    pass


class TestVersion3(make_test_case(3)):
    pass


class TestVersion4(make_test_case(4)):
    pass


class RegressionTest(unittest.TestCase):
    """
    Ensures that future versions of securefs can read old versions just fine.
    """

    def test_all(self):
        PLAIN_DATA_DIR = os.path.join(REFERENCE_DATA_DIR, "plain")
        for i in [1, 2, 3, 4]:
            mount_point = tempfile.mkdtemp(
                prefix="securefs.format{}.mount_point".format(i), dir="tmp",
            )
            # On Windows it is not possible to mount to an existing directory
            os.rmdir(mount_point)
            p = securefs_mount(
                os.path.join(REFERENCE_DATA_DIR, str(i)), mount_point, password="abc"
            )
            try:
                self.compare_directory(PLAIN_DATA_DIR, mount_point)
            finally:
                securefs_unmount(p, mount_point)

    def compare_directory(self, dir1, dir2):
        listing1 = list_dir_recursive(dir1, relpath=True)
        listing2 = list_dir_recursive(dir2, relpath=True)

        self.assertEqual(
            listing1, listing2, f"{dir1} and {dir2} differ in file names",
        )

        for fn in listing1:
            fn1 = os.path.join(dir1, fn)
            fn2 = os.path.join(dir2, fn)

            if os.path.isdir(fn1) and os.path.isdir(fn2):
                continue

            with open(fn1, "rb") as f:
                data1 = f.read()
            with open(fn2, "rb") as f:
                data2 = f.read()
            self.assertEqual(data1, data2, f"{fn1} and {fn2} differ in contents")


def list_dir_recursive(dirname: str, relpath=False) -> Set[str]:
    # Note: os.walk does not work on Windows when crossing filesystem boundary.
    # So we use this crude version instead.
    try:
        sub_filenames = os.listdir(dirname)
    except OSError:
        return set()
    result = set()
    for fn in sub_filenames:
        fn = os.path.join(dirname, fn)
        result.add(fn)
        result.update(list_dir_recursive(fn))
    if relpath:
        expanded_dirname = os.path.realpath(dirname)
        return set(
            os.path.relpath(os.path.realpath(f), expanded_dirname) for f in result
        )
    return result


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    unittest.main()
