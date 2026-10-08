"""Windows Job Object containment for the explicitly selected native dev worker.

Assign the runner itself before launching any engines. Descendants inherit the
job association, while its non-inheritable handle belongs only to the runner.
An abruptly killed runner therefore kills its complete engine process tree.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
import threading


class BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                ('max_working_set', ctypes.c_size_t), ('active_limit', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', BasicLimits), ('io', IoCounters), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in ('user_time', 'kernel_time', 'period_user_time', 'period_kernel_time')] + [
        ('page_faults', wintypes.DWORD), ('total_processes', wintypes.DWORD),
        ('active_processes', wintypes.DWORD), ('terminated_processes', wintypes.DWORD)]


class WindowsJob:
    def __init__(self, lock_path):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            'GetCurrentProcess': ([], wintypes.HANDLE),
            'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        }
        for name, (args, returns) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, returns
        name = 'Local\\GuerrillaGpu-' + hashlib.sha256(str(lock_path).lower().encode()).hexdigest()[:24]
        self.handle = self.kernel.CreateJobObjectW(None, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if self.active_processes():
            self.kernel.CloseHandle(self.handle)
            raise RuntimeError('A previous native engine process is still terminating')
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.kernel.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.kernel.AssignProcessToJobObject(self.handle, self.kernel.GetCurrentProcess()):
            self.kernel.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())
        self.stop = threading.Event()
        self.parent = None
        parent_pid = os.environ.get('WORKER_PARENT_PID')
        if parent_pid:
            self.parent = self.kernel.OpenProcess(0x00100000, False, int(parent_pid))  # SYNCHRONIZE
            if not self.parent:
                self.kernel.TerminateJobObject(self.handle, 1)
                raise RuntimeError('Native worker parent no longer exists')
            threading.Thread(target=self.watch_parent, daemon=True).start()

    def active_processes(self):
        accounting = Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.active_processes

    def watch_parent(self):
        while not self.stop.wait(0.25):
            if self.kernel.WaitForSingleObject(self.parent, 0) == 0:
                self.kernel.TerminateJobObject(self.handle, 1)
                return

    def close(self):
        self.stop.set()
        if self.active_processes() > 1:
            # A descendant escaped ordinary subprocess reaping. Do not release
            # the GPU lease while it lives: terminate the complete job instead.
            self.kernel.TerminateJobObject(self.handle, 1)
            return
        limits = ExtendedLimits()
        self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        self.kernel.CloseHandle(self.handle)
        if self.parent:
            self.kernel.CloseHandle(self.parent)
