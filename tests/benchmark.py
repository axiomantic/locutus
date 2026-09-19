#!/usr/bin/env python3
"""
Empirical Benchmark Suite for Locutus.
Measures binary size, process startup time, end-to-end message dispatch, and E2EE roundtrip.
"""

import os
import subprocess
import time
import statistics

if os.name == "nt":
    BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "locutus.exe"))
else:
    BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "locutus"))
REDIS_URL = os.environ.get("LOCUTUS_REDIS_URL", "redis://127.0.0.1:6379")
PREFIX = "locutus_bench:"
PROJECT = "bench"

env = os.environ.copy()
env["LOCUTUS_REDIS_URL"] = REDIS_URL
env["LOCUTUS_REDIS_PREFIX"] = PREFIX
env["LOCUTUS_PROJECT"] = PROJECT


def run_benchmark():
    print("==================================================")
    print(" Locutus Empirical Performance Benchmarks")
    print("==================================================")

    # 1. Binary Size
    size_bytes = os.path.getsize(BIN_PATH)
    print(f"1. Standalone Binary Size: {size_bytes / 1024:.1f} KB ({size_bytes} bytes)")

    # 2. Process Startup Time (--help)
    start_times = []
    for _ in range(50):
        t0 = time.perf_counter()
        subprocess.run([BIN_PATH, "--help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        start_times.append((time.perf_counter() - t0) * 1000)
    print(f"2. Cold Process Startup: median={statistics.median(start_times):.2f}ms, min={min(start_times):.2f}ms, p95={sorted(start_times)[int(len(start_times)*0.95)]:.2f}ms")

    # 3. End-to-end `locutus send` (HMAC + JSON + EVALSHA)
    subprocess.run([BIN_PATH, "open", "bench_agent", "worker"], env=env, stdout=subprocess.DEVNULL)
    send_times = []
    for i in range(50):
        t0 = time.perf_counter()
        subprocess.run([BIN_PATH, "send", "--to", "bench_agent", "--subject", "Bench", "--body", f"Task payload {i}"], env=env, stdout=subprocess.DEVNULL)
        send_times.append((time.perf_counter() - t0) * 1000)
    print(f"3. End-to-End Send Dispatch: median={statistics.median(send_times):.2f}ms, min={min(send_times):.2f}ms")

    # 4. Optional E2EE 150KB Payload (Encrypt + Redis + Decrypt)
    large_body = "Line of code: var x = 12345;\n" * 5000  # ~145 KB
    env["LOCUTUS_ENCRYPT"] = "1"
    e2ee_times = []
    for _ in range(10):
        t0 = time.perf_counter()
        subprocess.run([BIN_PATH, "send", "--to", "bench_agent", "--subject", "Large", "--body", large_body], env=env, stdout=subprocess.DEVNULL)
        subprocess.run([BIN_PATH, "listen", "bench_agent", "1"], env=env, stdout=subprocess.DEVNULL)
        e2ee_times.append((time.perf_counter() - t0) * 1000)
    print(f"4. E2EE 150KB Send + Listen: median={statistics.median(e2ee_times):.2f}ms, min={min(e2ee_times):.2f}ms")

    # Cleanup
    subprocess.run([BIN_PATH, "close", "bench_agent"], env=env, stdout=subprocess.DEVNULL)
    print("==================================================")


if __name__ == "__main__":
    run_benchmark()
