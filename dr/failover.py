"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def state_of(region: str) -> dict:
    """Đọc trạng thái /v1/state của region."""
    try:
        r = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
        if r.status_code == 200:
            return r.json()
        return {"error": f"status_{r.status_code}"}
    except Exception as e:
        return {"error": type(e).__name__}


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def failover(target: str, backend: str, wait: float = 60.0) -> dict:
    """5 bước failover đúng thứ tự nghiêm ngặt."""
    # Bước 1: 1_verify_target
    st = state_of(target)
    emit(step="1_verify_target", target=target, state=st)

    # Bước 2: 2_restore_snapshot
    meta = snapshot.get(target, backend)
    primary_region = "a" if target == "b" else "b"
    primary_db = pathlib.Path(f"state/region-{primary_region}/vectors.sqlite")
    target_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    rpo_info = snapshot.rpo(primary_db, target_db)
    rpo_s = rpo_info.get("rpo_seconds")
    docs_lost = rpo_info.get("docs_lost")
    embed_version = meta.get("embed_model_version")
    emit(
        step="2_restore_snapshot",
        target=target,
        backend=backend,
        rpo_seconds=rpo_s,
        docs_lost=docs_lost,
        embed_model_version=embed_version,
        snapshot_meta=meta,
    )

    # Bước 3: 3_scale_pool
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full\n")
    emit(step="3_scale_pool", target=target, pool_state="full")

    # Bước 4: 4_wait_ready
    t_start_wait = time.time()
    is_ready = False
    poll_interval = 0.5
    while time.time() - t_start_wait <= wait:
        try:
            r = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if r.status_code == 200:
                is_ready = True
                break
        except Exception:
            pass
        time.sleep(poll_interval)

    waited_s = round(time.time() - t_start_wait, 2)
    if not is_ready:
        emit(step="4_wait_ready", target=target, ready=False, waited_s=waited_s, error="timeout_waiting_for_ready")
        return {"ok": False, "target": target, "error": "target_not_ready_timeout", "waited_s": waited_s}

    emit(step="4_wait_ready", target=target, ready=True, waited_s=waited_s)

    # Bước 5: 5_dns_cutover
    active_file = pathlib.Path("edge/active_region")
    active_file.parent.mkdir(parents=True, exist_ok=True)
    active_file.write_text(f"{target}\n")
    emit(step="5_dns_cutover", target=target, active_region=target)

    return {
        "ok": True,
        "target": target,
        "rpo_seconds": rpo_s,
        "docs_lost": docs_lost,
        "embed_model_version": embed_version,
        "waited_s": waited_s,
        "state": state_of(target),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
