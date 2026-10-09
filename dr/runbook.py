"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK [{n}] {name}:", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook on-call."""
    t_start = time.time()

    # Lay moc t_outage tu chaos-events de dong bo cua so thoi gian
    t_outage = None
    chaos_file = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_file.exists():
        kills = [json.loads(l) for l in chaos_file.read_text().splitlines() if l.strip()]
        kill_events = [e for e in kills if e.get("action") == "kill"]
        if kill_events:
            t_outage = kill_events[-1].get("ts")

    # 1. xac_nhan_outage — cho alert tu health check hoac probe xac nhan
    # Dam bao operator chi cutover sau khi he thong da bat duoc su co (t_cutover >= t_detect)
    health_log = pathlib.Path("reports/health-events.jsonl")
    prim_dead = False
    deadline = time.time() + 25.0
    while time.time() < deadline:
        if health_log.exists():
            lines = [json.loads(l) for l in health_log.read_text().splitlines() if l.strip()]
            unhealthy_events = [
                e for e in lines
                if e.get("event") == "state_change"
                and e.get("to") == "UNHEALTHY"
                and e.get("region") == primary
                and (t_outage is None or e.get("ts") >= t_outage)
            ]
            if unhealthy_events:
                prim_dead = True
                break
        try:
            r = httpx.get(f"{URL[primary]}/readyz", timeout=1.0)
            if r.status_code != 200:
                prim_dead = True
        except Exception:
            prim_dead = True
        if prim_dead and not health_log.exists():
            break
        time.sleep(0.5)

    step(1, "xac_nhan_outage", primary=primary, primary_down=prim_dead, target=target)

    # 2. thong_bao_incident
    ts_alert = time.time()
    notice_latency_s = round(ts_alert - t_outage, 2) if t_outage else None
    step(2, "thong_bao_incident", t_outage=t_outage, ts_alert=ts_alert, notice_latency_s=notice_latency_s)

    # 3. scale_gpu_pool
    if not confirm(auto, f"Xac nhan failover tu region {primary} sang {target}?"):
        step(3, "scale_gpu_pool", confirmed=False, status="aborted_by_operator")
        return {"ok": False, "aborted": True}

    fo_res = fo.failover(target=target, backend=backend)
    step(3, "scale_gpu_pool", failover_ok=fo_res.get("ok"), target=target)

    # 4. verify_state_replica
    st = fo_res.get("state", {})
    step(
        4,
        "verify_state_replica",
        target=target,
        vector_count=st.get("count"),
        weights=st.get("weights"),
        rpo_seconds=fo_res.get("rpo_seconds"),
        docs_lost=fo_res.get("docs_lost"),
        embed_model_version=fo_res.get("embed_model_version"),
    )

    # 5. dns_cutover
    step(5, "dns_cutover", target=target, cutover_ok=fo_res.get("ok"))

    # 6. verify_golden_signals
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for i in range(10):
            t0 = time.time()
            try:
                r = client.get("http://127.0.0.1:8080/v1/infer", params={"q": f"ticket {i}"})
                if r.status_code == 200:
                    latencies.append((time.time() - t0) * 1000)
                else:
                    errors += 1
            except Exception:
                errors += 1
            time.sleep(0.05)

    latencies.sort()
    p95 = round(latencies[int(len(latencies) * 0.95)] if latencies else 0.0, 1)
    error_rate = round(errors / 10.0, 2)
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=p95, error_rate=error_rate)

    # 7. post_incident
    elapsed_s = round(time.time() - t_start, 2)
    measure_cmd = "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    step(7, "post_incident", elapsed_s=elapsed_s, measure_cmd=measure_cmd)

    return {
        "ok": fo_res.get("ok", False),
        "primary": primary,
        "target": target,
        "elapsed_s": elapsed_s,
        "failover": fo_res,
        "golden_signals": {"p95_ms": p95, "error_rate": error_rate},
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
