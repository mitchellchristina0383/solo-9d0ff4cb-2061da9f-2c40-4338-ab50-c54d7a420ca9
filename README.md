# 容器网关发布系统

一套可启动的 Docker Compose 环境：Flask 发布页 + HTTP 网关 + 两个样例后端。
发布页与网关共用同一个配置卷，发布时整表替换路由配置；网关每个请求在入口处
取一次配置快照，在途请求不受后续发布影响。两个样例后端还各自实现了一套
`sid` 登录，可通过网关演示“双登录、单边退出、改投迟到响应”的会话行为。

## 启动

```bash
docker compose up -d --build
```

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 发布页 | http://localhost:8081/ | 填写后端表 + 路由表，发布；延时试请求 |
| 网关 | http://localhost:8080/ | 按配置卷中的路由转发；`/__gateway/status` 查看现役配置 |
| 样例 A | http://localhost:9001/ | Compose 中的 `sample-a` |
| 样例 B | http://localhost:9002/ | Compose 中的 `sample-b` |

首次启动时 `config-init` 会准备共享卷并写入版本 1 的示例配置：
`/sample-a` → `sample-a`，`/sample-b` → `sample-b`。

```bash
curl -i http://localhost:8080/sample-a/hello   # 200，X-Gateway-Backend: sample-a
curl -i http://localhost:8080/nope             # 404 route_not_found
```

## 样例登录页（从网关打开）

两个样例都是“记住登录靠 `sid` Cookie”的旧应用，页面入口在网关路径下：

| 页面 | 样例 A | 样例 B |
| --- | --- | --- |
| 首页（登录状态 + 入口） | http://localhost:8080/sample-a/ | http://localhost:8080/sample-b/ |
| 登录（可设延时） | `/sample-a/login` | `/sample-b/login` |
| 查看会话（含浏览器送来的 sid 顺序） | `/sample-a/session` | `/sample-b/session` |
| 退出 | `/sample-a/logout` | `/sample-b/logout` |

- **各自独立的 `sid`**：登录响应的 `Set-Cookie` 不带 `Domain`，只带显式
  `Path`（`/sample-a` 或 `/sample-b`）。浏览器按“名称 + 路径”区分两个同名
  `sid`，因此两个样例可以同时登录；在 `/sample-a/logout` 退出只会清掉
  `Path=/sample-a` 的 `sid`，样例 B 的登录保持不变。
- **过期行为保留**：勾选“记住我”时下发带 `Max-Age`/`Expires`（7 天）的持久
  Cookie；不勾选则只下发会话级 Cookie（无 `Max-Age`/`Expires`），原样透传。
- **可延迟的登录响应**：登录表单可填延时毫秒，也可直接
  `POST /sample-a/login?delay_ms=3000`，用于在登录请求在途时改投路由。
- **路径来源**：样例从网关注入的 `X-Forwarded-Prefix` 请求头取得当前路由
  前缀作为 Cookie 的 `Path`；直连样例（不经网关）时回退到环境变量
  `COOKIE_PATH`，再回退到 `/`。路由被改投到新前缀后，新登录的 Cookie Path
  跟着新前缀变化。

## 网关的 Cookie 处理规则

- **只转发、不记账**：浏览器的 `Cookie` 头按原样转发。浏览器总是把多个
  Cookie 折叠成一行（以 `; ` 分隔），网关合并重复头行时对 `Cookie` 用
  `; ` 连接，因此浏览器送来的多个 `sid` 严格保持原顺序（见
  `/sample-a/hello` 响应里的 `sid_order`）。注：WSGI 服务器（gunicorn）
  在应用之前会按 PEP 3333 用 `, ` 合并极少数“多行 `Cookie`”的非浏览器
  请求，真实浏览器不受影响。
- **上游 Cookie 不落盘到连接池**：网关复用上游 TCP 连接（每线程一个
  `requests.Session`），但会话使用 `TransientCookieJar`——上游 `Set-Cookie`
  永不存入连接池。每次转发只带当前浏览器本次请求的 `Cookie`，一个浏览器的
  `sid` 绝不会通过复用的连接泄漏给另一个浏览器或另一个后端。
- **`Set-Cookie` 原样回传**：上游响应的每一条 `Set-Cookie`（含 `Path`、
  `Max-Age`、`Expires`、`HttpOnly`、`SameSite` 等属性，且保持“无
  `Domain`”）逐字节透传给浏览器，网关不增删改写属性。
- **改投时的迟到 `Set-Cookie` 仍归原后端**：每个请求在入口取一次不可变配置
  快照。慢登录在途期间把路径改投到别的后端，迟到的响应仍由原后端产生、
  仍带原后端的 `sid` 与原路径的 `Path`；把路径切回原后端后，这次登录的
  会话立即可读。
- 网关额外注入 `X-Forwarded-Prefix`（命中路由的前缀）、`X-Forwarded-For`、
  `X-Forwarded-Proto`、`X-Forwarded-Host`、`X-Request-Id`。

## 关键行为

- **整表发布**：发布页一次提交完整的后端表与路由表，校验通过后写入整个配置，
  不做逐条合并。每次写入 `version` 自增。
- **后端选自 Compose**：后端名必须是 `BACKEND_CATALOG` 中的样例服务
  （`sample-a` / `sample-b`），且地址必须与 Compose 声明一致；服务端校验，
  不依赖网页下拉框。
- **无效配置不生效**：路由引用不存在的后端、路径不合法或重复、后端名不在
  样例列表等，发布页返回 400 并列出全部错误，磁盘文件与现役配置均不变。
- **写入失败不生效**：写入使用同目录临时文件 + `fsync` + `os.replace` 原子替换。
  失败时发布页返回 500，原文件保持不变。
- **网关按快照转发**：每个请求在 `before_request` 取一次不可变配置快照，
  路由匹配与后端选择都基于该快照；发布只替换引用，不修改在途请求使用的对象。
- **撤下路径**：新配置中不存在的路径对新请求立即返回 404
  （`{"error":"route_not_found"}`），在途请求继续用旧快照完成。
- **坏配置兜底**：网关读取或校验配置失败时记录错误并继续使用上一次成功加载的
  配置；`/__gateway/status` 的 `config_file_error` 会显示原因。
- **延时试请求**：发布页向网关发请求并带上 `?delay_ms=N`，样例后端在处理期间
  睡眠 N 毫秒；结果显示状态码、实际后端（`X-Gateway-Backend`）、命中的路由
  （`X-Gateway-Route`）和使用的配置版本（`X-Gateway-Config-Version`）。
  因此可以在一次慢请求进行期间发布新配置，观察“新请求 404 / 在途请求 200”。

## 配置格式

共享卷中的 `/data/routes.json`：

```json
{
  "schema_version": 1,
  "version": 7,
  "published_at": "2026-11-24T12:00:00+00:00",
  "backends": [
    {"name": "sample-a", "url": "http://sample-a:9000"},
    {"name": "sample-b", "url": "http://sample-b:9000"}
  ],
  "routes": [
    {"path": "/api", "match": "prefix", "backend": "sample-a"},
    {"path": "/health", "match": "exact", "backend": "sample-b"}
  ]
}
```

匹配规则：`exact` 只匹配完全相同的路径；`prefix` 匹配该路径本身及其
`/` 子路径（`/api` 不会匹配 `/apifoo`）；多条命中时取最长路径。

## 配置项（`x-app-env`，见 `docker-compose.yml`）

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `CONFIG_PATH` | `/data/routes.json` | 共享卷中的配置文件 |
| `BACKEND_CATALOG` | 两个样例服务 | 允许的后端名 → 地址（JSON） |
| `BACKEND_LABELS` | 中文名 | 发布页显示名（JSON） |
| `UPSTREAM_CONNECT_TIMEOUT` / `UPSTREAM_READ_TIMEOUT` | 3 / 150 秒 | 网关到后端超时 |
| `PROBE_TIMEOUT` | 180 秒 | 发布页试请求超时 |
| `COOKIE_PATH`（仅样例） | `/sample-a`、`/sample-b` | 直连样例时 `sid` Cookie 的回退 Path；经网关时由 `X-Forwarded-Prefix` 决定 |

## 目录结构

```
common/      共享配置模型、原子存储、初始化脚本
gateway/     Flask 反向代理 + 配置快照存储 + 透传型 Cookie 处理
publisher/   发布页（Flask + 服务端校验 + 延时试请求）
samples/     可自报身份、可延时、带 sid 登录/会话/退出的样例后端
tests/       pytest 单元/集成测试
```

## 本地测试

```bash
python3 -m pytest -q tests
```

测试覆盖：配置校验与路由匹配、原子写入与并发读取、网关转发/404/在途请求/
坏配置兜底/后端不可达、`Set-Cookie` 原样透传（无 Domain、显式 Path、保留
过期）、多个 `sid` 按序转发、连接池不泄漏上游 Cookie、单边退出不影响另一边、
改投后迟到 `Set-Cookie` 仍归原后端，以及样例的登录/会话/退出/延时登录。

## 已知限制

- 发布页的试请求任务记录保存在进程内存，发布页重启后丢失（不影响配置）。
- 配置卷为 Docker 命名卷，多发布页实例写同一文件时靠文件锁串行化版本号，
  但本项目仅运行一个发布页。
- 样例的登录会话保存在进程内存，因此 Compose 中每个样例只跑 1 个 gunicorn
  worker（多线程）；重启样例会清空登录状态。
- 网关只支持 HTTP 后端，不终止 TLS；鉴权、限流不在本任务范围内。
