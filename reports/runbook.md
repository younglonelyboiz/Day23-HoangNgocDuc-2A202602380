# Runbook 1 trang — Region chính down

Runbook xử lý sự cố Region A bị cô lập / tê liệt hoàn toàn, failover sang Region B. Chạy được lúc 3h sáng bởi kỹ sư on-call.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `curl -s localhost:8001/readyz` hoặc kiểm tra `tail -n 5 reports/health-events.jsonl` | Region A trả về `UNHEALTHY` / ReadTimeout ≥ 3 lần liên tiếp; Region B alive | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `python3 -c "import time; print('INCIDENT_OPEN:', time.time())"` (hoặc ghi vào Incident Tracker) | Timestamp bắt đầu được ghi nhận, Slack channel sự cố được kích hoạt | Incident Commander (IC) |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | Lệnh kết thúc thành công, in metadata snapshot (model weights + vectors.sqlite) | On-call SRE / Automation |
| 4 | Scale pool warm→full | `echo "full" > state/region-b/pool_state && curl -s localhost:8002/readyz` | Endpoint `localhost:8002/readyz` trả HTTP 200, `"ready": true` | On-call SRE / Automation |
| 5 | DNS/LB cutover | `echo "b" > edge/active_region && curl -s localhost:8080/edge/state` | `curl localhost:8080/edge/state` trả về `"active_region": "b"` | On-call SRE / Automation |
| 6 | Verify golden signals | `for i in {1..10}; do curl -s localhost:8080/v1/infer; done` | 10 request liên tiếp trả về HTTP 200 với `"region": "b"`, p95 < 100ms, error rate 0% | On-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Log in ra `"rto_verdict": "PASS"`, thu thập đủ số liệu cho postmortem | IC & SRE |

*Lưu ý: Có thể thực thi toàn bộ các bước từ 1 đến 7 tự động hóa an toàn thông qua lệnh:*
```bash
python3 dr/runbook.py --primary a --target b --backend fs
```

---

### Quy trình Rollback (Failover ngược về Region A)

1. **Điều kiện tiên quyết để xem xét rollback:**
   - Region A đã khôi phục hoàn toàn: tiến trình FastAPI lắng nghe bình thường, `/healthz` trả về 200.
   - Đồng bộ ngược dữ liệu (reverse replication): Toàn bộ dữ liệu mới được ghi tại Region B trong thời gian sự cố đã được đồng bộ snapshot trở lại Region A (`python3 state/snapshot.py put --region b --backend fs && python3 state/snapshot.py get --region a --backend fs`).
   - Region A pool_state đã chuyển sang `full` và `/readyz` của Region A trả về 200 liên tục trong ít nhất 15 phút không có lỗi.
2. **Quyền hạn quyết định (Authority):**
   - Duy nhất **Incident Commander (IC)** hoặc **Tech Lead** có thẩm quyền ra quyết định phê duyệt rollback.
   - Tuyệt đối không để hệ thống tự động rollback (tránh anti-pattern flapping: mạng dao động làm 2 region liên tục chuyển giao qua lại gây nghẽn và corrupt dữ liệu).
3. **Thao tác rollback:**
   - Kỹ sư on-call gõ lệnh thủ công: `echo "a" > edge/active_region`.
   - Giám sát traffic 15 phút tiếp theo bằng dashboard Edge Proxy để xác nhận lưu lượng trở lại bình thường.
