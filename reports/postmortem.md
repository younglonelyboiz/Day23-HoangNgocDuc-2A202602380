# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T08:25:54 | outage bắt đầu (Region A bị netblock) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T08:25:54 | user đầu tiên bị ảnh hưởng (ReadTimeout 503) | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T08:26:09 | health check alert (Region A UNHEALTHY sau 3 lần fail) | `reports/health-events.jsonl:2` |
| 2026-10-09T08:26:10 | operator confirm cutover (kích hoạt failover pipeline) | `reports/runbook-run.jsonl:2` |
| 2026-10-09T08:26:16 | resolved (request đầu tiên OK được phục vụ bởi Region B) | `reports/drill-2-withdr.jsonl:36` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `22.2s` · gap: `-277.8s` (đáp ứng tiêu chuẩn an toàn RTO ≤ 300s)
- RPO mục tiêu: 300s · đo được: `2.0s` (`1` doc bị mất) · gap: `-298.0s` (đáp ứng tiêu chuẩn an toàn RPO ≤ 300s)
- **Bước tốn nhiều giây nhất:** `Health-check detection floor (15.0s)` — chiếm 67.6% tổng RTO. Nguyên nhân vì hệ thống bắt buộc kiểm tra 3 lần probe thất bại liên tiếp với chu kỳ 5 giây để tránh hiện tượng chuyển vùng giả (flapping).

## 3. Root cause (5 whys)

1. **Tại sao user không truy vấn được dịch vụ AI?**
   Edge Proxy không nhận được phản hồi từ Region A do cổng kết nối mạng bị chặn (`netblock` / dropped packets).
2. **Tại sao Edge Proxy không tự động định tuyến sang Region B ngay lập tức?**
   Edge Proxy là tầng không có trạng thái phức tạp, định tuyến phụ thuộc vào con trỏ DNS/active_region và bộ đệm cache TTL (5s).
3. **Tại sao cần mất 15 giây để xác định Region A bị sập?**
   Cơ chế Health Checker áp dụng nguyên lý chống flapping: yêu cầu 3 chu kỳ thăm dò thất bại liên tiếp (5s/lần) trước khi nâng mức cảnh báo thành `UNHEALTHY`.
4. **Tại sao Region B không sẵn sàng phục vụ ngay khi có sự cố?**
   Region B được thiết kế theo mô hình Pilot Light / Warm Standby để tiết kiệm chi phí GPU: vector database chỉ đồng bộ theo chu kỳ snapshot 30s và GPU pool ở trạng thái `warm` thay vì `full`.
5. **Nếu đây là sự cố thực tế trên production, bước nào trong runbook có nguy cơ thất bại cao nhất?**
   Bước `2_restore_snapshot` và `4_wait_ready`. Với các hệ thống AI thực tế có cơ sở dữ liệu vector hàng trăm GB và model weights cỡ 70B tham số (hàng trăm GB), thời gian kéo snapshot qua mạng WAN và thời gian tải trọng số mô hình vào GPU VRAM (cold/warm-up) có thể kéo dài hàng chục phút nếu không có pre-warmed GPU pool và distributed storage caching.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Nâng cấp cơ chế đồng bộ vector DB từ snapshot định kỳ 30s sang Change Data Capture (CDC) / WAL streaming liên tục | Data Infra Team | 2026-11-01 | Giảm RPO từ 2.0s xuống < 0.2s, triệt tiêu nguy cơ mất documents |
| 2 | Duy trì GPU Warm Standby Pool đã nạp sẵn trọng số mô hình vào VRAM ở Region B | MLOps Team | 2026-10-25 | Giảm 6.3s warm-up time trong RTO |
| 3 | Tích hợp Route53 / Cloudflare Anycast health check với circuit breaker tự động | SRE Team | 2026-11-10 | Giảm độ trễ DNS cache TTL xuống < 1s |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - Cấu hình: `interval = 5.0s`, `threshold = 3` → Detection floor = `15.0s`.
   - Tổng RTO đo được là `22.2s`. Tỷ lệ chiếm dụng: `15.0 / 22.2 = 67.6%` tổng thời gian phục hồi.
2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - Với `interval = 1.0s` và `threshold = 3`, detection floor giảm xuống `3.0s` → RTO giảm `12.0s`.
   - **Cái giá phải trả**: Nguy cơ **flapping** (chuyển vùng giả). Khi mạng chỉ bị trễ tức thời hoặc rớt gói tin tạm thời trong 3 giây, hệ thống sẽ vội vàng kích hoạt failover. Việc chuyển vùng sai dẫn đến gián đoạn kết nối của người dùng đang hoạt động, phát sinh chi phí scale tài nguyên GPU không cần thiết và tiềm ẩn nguy cơ bất nhất dữ liệu hai vùng.
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - Trong đợt drill, `docs_lost = 1 document` (dữ liệu được nạp vào Region A trong 2.0s giữa lần snapshot cuối và lúc xảy ra sự cố).
   - Nếu Region A mất vĩnh viễn, những documents này bị mất vĩnh viễn nếu không có bản lưu ngoài. Đối với khách hàng, điều này đồng nghĩa với việc các giao dịch/ticket vừa gửi đi trong 2 giây đó không bao giờ được ghi nhận. Để khắc phục, hệ thống cần có transaction log đệm (như Kafka topic multi-region replica) để replay lại các giao dịch bị gián đoạn.
