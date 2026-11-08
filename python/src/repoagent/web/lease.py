"""一个输出目录只允许一个正式Web进程；崩溃时操作系统自动释放锁。"""

import os


class DirectoryLease:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ".web-runtime.lock"
        if path.is_symlink():
            raise ValueError("Refuse runtime lock symlink")
        self.stream = path.open("a+b")
        self.stream.seek(0)
        if os.fstat(self.stream.fileno()).st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.stream.close()
            raise ValueError("Output directory already in use") from None

    def close(self):
        self.stream.close()
