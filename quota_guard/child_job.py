"""Tie the application-owned networking child to this Windows process lifetime."""
import ctypes
from ctypes import wintypes


class Basic(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64), ('flags', wintypes.DWORD),
                ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('processes', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]


class Limits(ctypes.Structure):
    _fields_ = [('basic', Basic), ('io', ctypes.c_uint64*6), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


class ChildJob:
    def __init__(self, process):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.handle = kernel.CreateJobObjectW(None, None)
        settings = Limits()
        settings.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.SetInformationJobObject(wintypes.HANDLE(self.handle), 9, ctypes.byref(settings), ctypes.sizeof(settings)) \
                or not kernel.AssignProcessToJobObject(wintypes.HANDLE(self.handle), wintypes.HANDLE(int(process._handle))):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)
        if ctypes.windll.ntdll.NtResumeProcess(wintypes.HANDLE(int(process._handle))) != 0:
            self.close()
            raise RuntimeError('无法启动自动连接组件')

    def close(self):
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(self.handle))
            self.handle = None
