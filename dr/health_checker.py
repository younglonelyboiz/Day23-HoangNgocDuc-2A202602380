"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    url = f"{URL[region]}/readyz"
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code == 200:
            return True, "ready"
        else:
            try:
                body = r.json()
                reasons = body.get("reasons", [])
                reason_str = ", ".join(reasons) if reasons else f"status_{r.status_code}"
            except Exception:
                reason_str = f"status_{r.status_code}"
            return False, reason_str
    except Exception as e:
        return False, type(e).__name__


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL."""
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    end = time.time() + duration
    state = {"a": "HEALTHY", "b": "HEALTHY"}
    consecutive_fails = {"a": 0, "b": 0}

    with out.open("a") as f:
        while time.time() < end:
            loop_start = time.time()
            for r in ["a", "b"]:
                ok, reason = probe(r, timeout)
                if not ok:
                    consecutive_fails[r] += 1
                    if consecutive_fails[r] >= threshold and state[r] == "HEALTHY":
                        old_st = state[r]
                        state[r] = "UNHEALTHY"
                        rec = {
                            "ts": time.time(),
                            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                            "event": "state_change",
                            "region": r,
                            "from": old_st,
                            "to": "UNHEALTHY",
                            "reason": reason,
                            "consecutive_fails": consecutive_fails[r],
                            "interval_s": interval,
                            "threshold": threshold,
                        }
                        f.write(json.dumps(rec) + "\n")
                        f.flush()
                        print("HEALTH", json.dumps(rec))
                else:
                    if state[r] == "UNHEALTHY":
                        old_st = state[r]
                        state[r] = "HEALTHY"
                        rec = {
                            "ts": time.time(),
                            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                            "event": "state_change",
                            "region": r,
                            "from": old_st,
                            "to": "HEALTHY",
                            "reason": reason,
                            "consecutive_fails": 0,
                            "interval_s": interval,
                            "threshold": threshold,
                        }
                        f.write(json.dumps(rec) + "\n")
                        f.flush()
                        print("HEALTH", json.dumps(rec))
                    consecutive_fails[r] = 0

            elapsed = time.time() - loop_start
            sleep_time = max(0.0, interval - elapsed)
            time.sleep(sleep_time)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
