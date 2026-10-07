# Hệ thống quản lý và điều phối ứng phó sự cố khẩn cấp theo thời gian thực

**Real-time Emergency Response Management and Dispatch System**

**Production / Live demo:** [https://vjettejv.id.vn](https://vjettejv.id.vn)

Ứng dụng Web GIS kết nối người dân, điều phối viên và đội ứng cứu. Người dân gửi báo cáo kèm vị trí, thông tin liên hệ và media hiện trường; điều phối viên xác minh, liên kết các báo cáo liên quan thành sự cố và phân công đội phù hợp.

GeoDjango/PostGIS hỗ trợ truy vấn theo khoảng cách; WebSocket cập nhật trạng thái sự cố, nhiệm vụ và vị trí đội trên bản đồ. Đây là sản phẩm đồ án, không phải dịch vụ tiếp nhận khẩn cấp chính thức 112/114/115.

## Chức năng chính

| Vai trò | Chức năng |
| --- | --- |
| **Citizen** | Gửi báo cáo; lấy GPS; chọn/chỉnh vị trí hiện trường trên Leaflet; tìm địa chỉ và reverse geocoding; chụp ảnh/quay video; gửi thông tin liên hệ; theo dõi báo cáo của mình. |
| **Dispatcher** | Tiếp nhận và xác minh báo cáo; quản lý bản đồ sự cố/báo cáo/đội; lọc và tìm kiếm; xem đề xuất báo cáo liên quan; link/merge hoặc bỏ qua đề xuất; gợi ý đội theo khả năng, độ sẵn sàng và khoảng cách; phân công/đổi/hủy nhiệm vụ; theo dõi GPS, trạng thái và timeline; liên hệ người báo tin theo quyền. |
| **Rescue Team** | Xem nhiệm vụ và vị trí hiện trường; nhận nhiệm vụ; cập nhật tiến độ; chia sẻ GPS; gọi người báo tin khi có sự đồng ý và nhiệm vụ còn hoạt động. |
| **Admin** | Quản lý người dùng/vai trò, loại sự cố và đội ứng cứu; tìm kiếm/lọc; vô hiệu hóa tài khoản; xem cấu hình nghiệp vụ hiện tại (chỉ đọc). |

Tiến trình nhiệm vụ qua API: `assigned → accepted → en_route → on_scene → responding → completed`. Trạng thái `assigned` được lưu nội bộ dưới tên `pending`.

Giao diện responsive dùng cùng API thật cho cả bốn vai trò. Khi mất kết nối, dữ liệu giữ lại được đánh dấu **Dữ liệu gần nhất**; sau reconnect, ứng dụng đồng bộ REST trước khi tiếp tục hiển thị cập nhật realtime.

## Quy trình nghiệp vụ

```mermaid
flowchart LR
    A[Citizen báo sự cố] --> B[IncidentReport]
    B --> C[Dispatcher xác minh]
    C --> D[Tạo hoặc gắn vào Incident]
    D --> E[Gợi ý đội và Dispatch]
    E --> F[Đội nhận và xử lý]
    F --> G[GPS và trạng thái realtime]
    G --> H[Hoàn thành và resolved]
```

Báo cáo tiềm năng trùng được tìm bằng loại sự cố, trạng thái, khoảng cách và khoảng thời gian cấu hình. Dispatcher quyết định liên kết/gom báo cáo; hệ thống không tự merge. Gợi ý đội dùng truy vấn không gian trên PostGIS, không quét toàn bộ dữ liệu bằng Python.

## Công nghệ

| Thành phần | Stack trong repository |
| --- | --- |
| Backend | Python 3.12 trong Docker; Django 5.2; Django REST Framework; GeoDjango |
| Database | PostgreSQL/PostGIS; local dùng image `postgis/postgis:16-3.4`, production dùng AWS RDS |
| Realtime | Django Channels, Daphne, Redis Channel Layer, WebSocket |
| Tác vụ nền | Celery Worker + một Celery Beat; Redis broker; tối ưu ảnh và dọn media với retry giới hạn |
| Frontend | Vanilla HTML/CSS/JavaScript, Leaflet 1.9.4, OpenStreetMap; không có npm build |
| Địa chỉ | Adapter Nominatim qua backend, cache và giới hạn tần suất; provider cấu hình được |
| Media | Amazon S3 private, boto3, presigned upload/download; Pillow tối ưu ảnh, giữ bản gốc |
| Thông báo | Inbox lưu trong database, read/unread và WebSocket riêng theo người nhận |
| Triển khai | AWS EC2, Docker Compose, Nginx, HTTPS/WSS; override CloudWatch tùy chọn |
| CI/CD | GitHub Actions, AWS OIDC/IAM, AWS Systems Manager Run Command |

Các phiên bản Python dependency được pin trong [backend/requirements.txt](backend/requirements.txt). Phase 8.5 bổ sung thông báo in-app và tối ưu ảnh. Chưa có tính tuyến đường/ETA, email, SMS, mobile push hoặc transcoding video.

## Kiến trúc

```mermaid
flowchart TD
    Client[Browser và Leaflet] -->|HTTPS / WSS| Nginx
    Nginx --> App[Django / DRF / Channels / Daphne]
    App --> DB[RDS PostgreSQL / PostGIS]
    App <--> Redis[Redis: channel layer, cache, broker]
    Redis --> Worker[Celery Worker]
    Beat[Celery Beat] --> Redis
    Worker --> DB
    Worker --> S3[Private S3]
    App -->|Presign và xác nhận metadata| S3
    Client -->|Upload trực tiếp bằng URL có hạn| S3
    Actions[GitHub Actions] --> OIDC[OIDC / IAM]
    OIDC --> SSM[SSM Run Command]
    SSM --> EC2[EC2 / Docker Compose]
```

Business logic nằm trong các service và transaction của backend. Thay đổi trạng thái/liên kết được ghi lịch sử; sự kiện realtime chỉ phát sau commit. WebSocket truyền sự kiện, còn CRUD, điều phối và cập nhật GPS dùng REST.

## Cấu trúc repository

```text
.
├── backend/
│   ├── config/             # Settings, ASGI, Celery, test runner
│   ├── accounts/           # Authentication, users, roles, seed_demo
│   ├── incidents/          # Reports, incidents, clustering, contacts, geocoding
│   ├── teams/              # Response teams và vị trí GPS
│   ├── dispatch/           # Assignment, suggestions, timeline, history
│   ├── evidence/           # Media lifecycle, S3 adapter, tối ưu ảnh và cleanup
│   ├── notifications/      # Inbox, read/unread, thông báo nghiệp vụ theo người nhận
│   ├── operations/         # REST quản trị
│   ├── common/             # Health, pagination, logging, shared utilities
│   ├── realtime/
│   │   └── frontend/       # index.html, app.js, client.js, map.js, style.css
│   ├── tests/              # Backend integration và frontend Node tests
│   ├── manage.py
│   └── requirements.txt
├── infra/                  # Dockerfile, Nginx, CI/deploy scripts, IAM templates
│   └── tests/              # Kiểm tra deployment và repository safety
├── .github/workflows/      # CI/CD và chẩn đoán OIDC thủ công
├── compose.yaml            # Local PostGIS, Redis, backend, worker, beat
├── compose.production.yaml # Production override, initialize, Nginx, RDS
├── compose.https.yaml      # HTTPS override
├── compose.cloudwatch.yaml # CloudWatch logging override tùy chọn
├── .env.example
└── .env.production.example
```

Migrations nằm trong từng module Django; không bỏ migrations khỏi version control. Tài liệu đồ án, ghi chú nội bộ và artifact kiểm tra không nằm trong source được push.

## Chạy local

Yêu cầu Docker với Compose; Compose **2.24.4 trở lên** nếu dùng các production override. Node.js 24 dùng để chạy test frontend, không cần để phục vụ giao diện.

Từ thư mục repository, tạo `.env` từ `.env.example` (PowerShell: `Copy-Item .env.example .env`; Linux/macOS: `cp .env.example .env`). Thay hai placeholder `DJANGO_SECRET_KEY` và `POSTGRES_PASSWORD` bằng giá trị local riêng, không commit `.env`.

```bash
docker compose build backend worker beat
docker compose up -d db redis
docker compose run --rm backend python manage.py migrate
docker compose run --rm backend python manage.py seed_categories
docker compose up -d backend worker beat
```

Mở [http://localhost:8000/](http://localhost:8000/) để xem landing public; [http://localhost:8000/login](http://localhost:8000/login) để đăng nhập. API readiness: [http://localhost:8000/api/health/](http://localhost:8000/api/health/).

Tạo tài khoản quản trị local khi cần:

```bash
docker compose exec backend python manage.py createsuperuser
```

### Tài khoản seed để thử nghiệm local

`seed_demo` tạo **`demo_citizen`**, **`demo_dispatcher`**, **`demo_rescue_team`**, **`demo_medical`**, **`demo_backup`**, **`demo_admin`** và ba đội với tọa độ/capability thử nghiệm. Mật khẩu lấy từ biến môi trường tạm `DEMO_PASSWORD`; không có mật khẩu mặc định trong repository, không công bố thông tin đăng nhập production. Có thể đăng ký Citizen qua giao diện.

Trên PowerShell, nhập mật khẩu seed mà không ghi giá trị vào lịch sử lệnh:

```powershell
$env:DEMO_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host 'Mật khẩu seed local' -AsSecureString)).Password
try {
    docker compose exec -e DEMO_PASSWORD backend python manage.py seed_demo
} finally {
    Remove-Item Env:DEMO_PASSWORD
}
```

Lệnh giữ nguyên tài khoản, mật khẩu và trạng thái đội đã tồn tại; command bị chặn khi `DJANGO_ENV=production`. Đội đã busy/offline cần được kiểm tra trước buổi demo, không tự reset bằng seed. Khi gửi báo cáo thử, nhập họ tên và số điện thoại thử nghiệm; seed không tự cung cấp thông tin liên hệ cho Citizen.

S3 cần bucket private và quyền truy cập hợp lệ để thử upload thật. Nếu chưa cấu hình, luồng media không hoạt động; không có storage giả thay thế. Camera/GPS cần quyền trình duyệt và secure context (localhost hoặc HTTPS).

## Biến môi trường

Danh sách đầy đủ và giá trị mẫu nằm trong [.env.example](.env.example) và [.env.production.example](.env.production.example).

| Nhóm | Biến chính |
| --- | --- |
| Django | `DJANGO_SECRET_KEY`, `DJANGO_ENV`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_HTTPS_ENABLED`, `CSRF_TRUSTED_ORIGINS`, `CORS_ALLOWED_ORIGINS` |
| Database | `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_SSLMODE`, `POSTGRES_SSLROOTCERT`; `POSTGRES_HOST_PORT` chỉ cho cổng local |
| Redis / Celery | `REDIS_URL`, `REDIS_CACHE_URL`, `REDIS_CHANNEL_PREFIX`, `CELERY_BROKER_URL`, `CELERY_TASK_DEFAULT_QUEUE` |
| Realtime / GPS | `WS_ALLOWED_ORIGINS`, `GPS_MIN_INTERVAL_SECONDS`, `GPS_MAX_AGE_SECONDS`, `GPS_FUTURE_TOLERANCE_SECONDS` |
| Không gian | `CLUSTER_RADIUS_METERS`, `CLUSTER_TIME_WINDOW_MINUTES`, `CLUSTER_MAX_CANDIDATES`, `DISPATCH_RADIUS_METERS`, `DISPATCH_MAX_SUGGESTIONS` |
| Bản đồ / camera | `LEAFLET_TILE_URL`, `GEOCODER_BASE_URL`, `GEOCODER_USER_AGENT`, `GEOCODER_CACHE_SECONDS`, `INCIDENT_LOCATION_DISTANCE_WARNING_METERS`, `CAMERA_VIDEO_MAX_SECONDS` |
| S3 / media | `AWS_REGION`, `S3_BUCKET_NAME`, `AWS_EC2_METADATA_DISABLED`, `MEDIA_MAX_BYTES`, `MEDIA_UPLOAD_TTL_SECONDS`, `MEDIA_DOWNLOAD_TTL_SECONDS`, `MEDIA_CLEANUP_GRACE_SECONDS` |
| Tối ưu ảnh | `MEDIA_IMAGE_MAX_DIMENSION`, `MEDIA_IMAGE_MAX_PIXELS`, `MEDIA_IMAGE_QUALITY`, `MEDIA_IMAGE_OUTPUT_FORMAT` (JPEG hoặc WEBP) |
| Production Compose | `COMPOSE_PROJECT_NAME`, `APP_IMAGE`, `APP_TAG`, `PUBLIC_HOST`, `HTTP_PORT`, `HTTPS_PORT`; `CLOUDWATCH_LOG_GROUP` khi bật override logging |

EC2 dùng IAM instance role; không cần access key trong app. Khi chạy local, boto3 có thể lấy credentials từ môi trường theo `.env.example`. Upload trực tiếp cần CORS S3 cho đúng origin; URL upload/download có thời hạn, giới hạn bởi cấu hình.

## API và realtime

REST base: `/api/v1/`. Xác thực dùng DRF Token trong header `Authorization: Token <token>`. Backend kiểm tra role/ownership cho từng thao tác; đăng ký luôn tạo Citizen.

| Nhóm | Endpoint chính |
| --- | --- |
| Authentication | `auth/register/`, `auth/login/`, `auth/logout/`, `auth/me/` |
| Báo cáo / loại sự cố | `incident-reports/`, `incident-reports/drafts/`, `incident-categories/` |
| Sự cố | `incidents/`; các action xác minh/link/merge/trạng thái nằm trong `incidents/views.py` |
| Điều phối | `incidents/<id>/suggested-teams/`, `incidents/<id>/assignments/`, `assignments/` |
| GPS | `teams/me/location/`, `teams/locations/` |
| Media | `media/presign/`, `media/`, `media/<uuid>/confirm/`, `media/<uuid>/download/`, `media/<uuid>/` |
| Thông báo | GET `notifications/`, `notifications/unread-count/`, `notifications/<id>/`; POST/PATCH `notifications/<id>/read/`, `notifications/read-all/` |
| Địa chỉ | `geocoding/search/`, `geocoding/reverse/` (POST, Citizen) |
| Admin | `admin/users/`, `admin/categories/`, `admin/teams/`, `admin/configuration/` |

Trang public ở `/`, login ở `/login` (giữ `/realtime/#/login` tương thích). Frontend dùng routes `/realtime/#/citizen/report`, `/realtime/#/dispatcher`, `/realtime/#/rescue`, `/realtime/#/admin/users`; sau login redirect theo role. Landing chỉ dùng sơ đồ SVG tĩnh; không lấy dữ liệu sự cố hay mở GPS/camera/WebSocket. Phiên đã xác thực có CTA “Vào hệ thống”.

WebSocket routes: **`/ws/dispatcher/`**, **`/ws/rescue/`**, **`/ws/notifications/`**. Client gửi token trong frame xác thực đầu tiên; server chọn group theo quyền. Các event nghiệp vụ gồm `incident.status_changed`, `assignment.status_changed`, `team.location_updated`, `report.changed`, `reports.linked`; client reconnect và đồng bộ REST khi kết nối lại. Inbox dùng group `user_<id>`, nhận `notification.created` và `notification.read` sau commit; chỉ người nhận có quyền xem hoặc đánh dấu đã đọc.

Media đi theo luồng **metadata → presigned URL → upload trực tiếp S3 → confirm → metadata database**. Backend xác nhận object/metadata/checksum trước khi cấp quyền tải. Sau confirm, Celery đọc bản gốc, kiểm tra ảnh, xoay EXIF, resize giữ tỉ lệ và tạo bản tối ưu private. Download ưu tiên bản tối ưu khi sẵn sàng, dùng bản gốc khi chưa xử lý hoặc lỗi; `?variant=original` lấy bản gốc theo cùng quyền. Task có tối đa ba lần xử lý và Beat phục hồi công việc bị gián đoạn. Media cũ giữ nguyên bản gốc. Repository chưa có Swagger/OpenAPI UI.

## Production và CI/CD

Production: [https://vjettejv.id.vn](https://vjettejv.id.vn), sử dụng EC2, Docker Compose, Nginx, Django/Daphne, Redis, Celery, RDS PostgreSQL/PostGIS và S3. Database dùng TLS `verify-full`; chỉ Nginx publish cổng ra ngoài. Phase 8.5–9 đã được triển khai ngày 07/10/2026 qua OIDC/SSM; trạng thái release vẫn chờ kiểm tra điện thoại thật.

Workflow [.github/workflows/ci-cd.yaml](.github/workflows/ci-cd.yaml) chạy khi push hoặc pull request vào `main`:

1. CI scan source/lịch sử Git, kiểm tra scripts hạ tầng, validate Compose, build Docker, Django check, migrate, kiểm tra thiếu migration, test backend và frontend.
2. Chỉ push `main` vượt qua CI mới chạy CD. GitHub nhận credentials tạm qua OIDC, gọi SSM và triển khai đúng commit đã test trên EC2.
3. Deployment build image, chạy migrations/collectstatic qua `initialize`, khởi động services và kiểm tra container/HTTPS readiness. Health check lỗi làm workflow fail; cơ chế khôi phục image không đảo ngược schema.

GitHub repository **Variables** đang được workflow sử dụng: `AWS_REGION`, `AWS_ROLE_ARN`, `EC2_INSTANCE_ID`, `DEPLOY_PATH`. Không cần SSH deployment key hoặc AWS access key dài hạn cho flow này. Environment production và chứng chỉ được giữ trên EC2, ngoài Git.

## Kiểm tra

```bash
docker compose config --quiet
docker compose run --rm backend python manage.py check
docker compose run --rm backend python manage.py makemigrations --check --dry-run
docker compose run --rm backend python manage.py migrate --plan
docker compose run --rm backend python manage.py test tests --noinput
node --test backend/tests/frontend_realtime.test.cjs backend/tests/frontend_landing.test.cjs
python -m unittest discover -s infra/tests
python infra/check_repository.py --history
```

PostGIS và Redis phải đang chạy cho backend integration tests; test runner cô lập dữ liệu Redis. S3 được kiểm tra bằng botocore Stubber; worker tests dùng queue riêng. Các test kiểm tra phân quyền, chuyển trạng thái, truy vấn không gian, concurrency, media, lỗi dịch vụ và reconnect/UI. Một số test deployment chỉ chạy trên Linux.

`infra/ci.sh` chạy toàn bộ pipeline trên checkout sạch, tự tạo môi trường test và yêu cầu `COMPOSE_PROJECT_NAME` dạng `ers-ci-*`; không chạy script này trên thư mục đang chứa `.env` phát triển. Frontend vanilla được phục vụ trực tiếp; CI chạy Node tests và `node --check` cho các file JavaScript, không có npm production build.

### Kết quả Phase 9 local — 07/10/2026

Backend **242/242**, frontend **62/62**, hạ tầng Linux **33/33** đã pass. Docker build, Django check, migration drift và Compose local/production overrides đều pass; local không còn migration chờ áp dụng. Các test gồm luồng REST + Redis WebSocket + notification + xử lý ảnh, race condition và phục hồi worker. S3 trong test được stub; chưa chạy production smoke cho bản này.

### Final release candidate — 07/10/2026

Chạy lại trên database test mới: **243/243 backend**, **62/62 frontend**, **33/33 infra Linux**, **216/216 regression** pass. Test bổ sung xác nhận JSON vượt giới hạn dung lượng bị chặn trước khi ghi báo cáo. Docker build, Django check, migration drift, JavaScript syntax, Compose và Nginx HTTP/HTTPS đều pass. DRF được cập nhật lên **3.17.2**, Daphne lên **4.2.2** để vá các advisory; `pip-audit` trên requirements và **51 dependency của Docker image** không còn finding. Frontend vanilla không có npm dependencies để audit.

Preflight production xác nhận hai migration chỉ thêm bảng Notification, các trường và index MediaAsset; deployment đã áp dụng thành công, không xóa hay đảo migration. Batch [3dae532](https://github.com/vjettejv/emergency-response-system/commit/3dae5328ae59e191e312d714f93b9a2ff655efdb) vượt qua [CI/CD](https://github.com/vjettejv/emergency-response-system/actions/runs/37586611629), S3 upload/confirm/private download/URL hết hạn, Celery optimization, PostGIS, WSS/notification/reconnect và golden path production với dữ liệu RELEASE TEST. Kiểm tra responsive phát hiện CSS cũ ẩn chuông thông báo Citizen/Dispatcher trên mobile; selector đã được sửa để giữ notification cho mọi role.

GPS/camera, keyboard và bản đồ nền trên điện thoại thật vẫn **MANUAL PENDING**; chưa tạo release tag. Tile nền chưa tải được trên máy kiểm tra, trong khi cùng URL trả HTTP 200 từ EC2; không coi viewport mô phỏng là bằng chứng thiết bị thật. Không reset volume hoặc tự rollback schema khi deployment lỗi sau migration.

Benchmark tùy chọn dùng database test riêng, không gọi S3 hay production:

```bash
docker compose run --rm --no-deps backend python manage.py test tests.release_benchmark --noinput
```

Mẫu local gồm 2.000 report, 500 incident, 300 team; 370 HTTP request qua Daphne không lỗi, 40 ASGI WebSocket qua Redis nhận đủ 800 lượt event. Đây là phép đo hữu hạn trên Docker local, không xác lập sức tải AWS/WSS hoặc SLA. Có thể mount thư mục ảnh thử nghiệm chỉ đọc vào `/bench-images` để đo ảnh local; không commit ảnh, log hoặc artifact.

## Bảo mật và phạm vi

- Secrets được lấy từ environment/IAM; không lưu mật khẩu, token hoặc khóa riêng trong Git.
- Role/ownership được backend xác thực; frontend guard chỉ hỗ trợ điều hướng.
- Contact được che ở danh sách; truy cập chi tiết và gọi điện phụ thuộc quyền, consent và nhiệm vụ.
- S3 private, key do server tạo; file type/kích thước/checksum được kiểm tra, URL có hạn.
- Production dùng HTTPS/WSS; WebSocket kiểm tra token, origin và nhóm được phép.
- OIDC thay credentials AWS dài hạn trong CI/CD; logging tránh in token, signed URL và dữ liệu liên hệ.
- Rate guard hiện có: auth 20/phút/IP, report write 120/phút/user, presign + confirm chung 300/phút/user, notification 600/phút/user; GPS có giới hạn khoảng cách thời gian theo đội. Cache rate guard lỗi trả 503; DRF throttle không đảm bảo quota nguyên tử dưới tải đồng thời.
- Nginx giới hạn 40 kết nối WebSocket đồng thời/IP; các người dùng chung NAT chia sẻ giới hạn này. Redis/DB gián đoạn đóng socket với mã retryable; client reconnect rồi đồng bộ REST.

Repository phục vụ đồ án và demo nghiệp vụ. Chưa có load test production cuối cùng, email/SMS/mobile push, định tuyến/ETA hay cam kết khả dụng của một dịch vụ khẩn cấp thực tế. Camera/GPS và bản đồ nền phụ thuộc thiết bị, quyền trình duyệt và mạng; S3 private/CORS/IAM cần smoke test trên đích triển khai trước release.
