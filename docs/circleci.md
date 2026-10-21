# CircleCI 日常检查迁移指南

本配置基于 `fengshuiling/Fudan_iCourse_Subscriber` 的 `main` 提交
`78ef4a47d1094f347eadfc7904ea32c811f784f4`。代码和加密数据库继续存储在 GitHub；
课程检查改由 CircleCI Cloud 免费 Linux Docker 执行器运行，无需自建 Runner。

**合并代码不等于启用抓取。** 普通提交仅运行离线检查。运行参数和两个环境开关默认关闭。
本次实施没有启动课程抓取、发送邮件、配置线上定时器或写入真实 `data` 分支。

## 1. 改动范围

| 文件/功能 | 处理方式 |
| --- | --- |
| `.circleci/config.yml` | Python 3.12.15；离线测试、只读历史验证、完整检查三条路径 |
| `.circleci/install_models.sh` | 原 SenseVoice 2024-07-17、Silero VAD；只缓存 int8 以节省空间 |
| `scripts/circleci_pipeline.py` | GH_PAT 读写历史、严格验证、旧格式兼容、复用分片/合并、快进推送 |
| `main.py` / `src/runtime/run_budget.py` | 可选课次数限制和软截止时间；未设置时保持原本无上限行为 |
| `src/runtime/config.py` | 兼容 `STUID`/`StuId` 和 `UISPSW`/`UISPsw` |
| `.github/workflows/check.yml` | 移除每日定时器，保留只显示迁移说明的手动入口；旧实现保存在 Git 历史 |
| `src/data/crypto_box.py`、`src/data/sharder.py`、`scripts/merge_db.py` | 保持加密格式、PSA 分片和字段合并逻辑不变 |
| `frontend/**` 和其他 GitHub 工作流 | 内容不变；未迁移功能见第 8 节 |

## 2. GitHub 最小权限令牌

GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens：

1. Resource owner 选择 `fengshuiling`。
2. Repository access 选择 **Only select repositories → Fudan_iCourse_Subscriber**。
3. Repository permissions 仅授予 **Contents: Read and write**；Metadata 只读自动包含。
4. 设置有效期，过期前在 CircleCI 更新 `GH_PAT`。

不需要 Actions、Secrets、Workflows、Pages 或 Administration 权限。
Fine-grained PAT 的 Contents 写权限是**仓库级**，不能限制成只允许 `data` 分支。
脚本只推送 `refs/heads/data`；需要更强隔离可另外保护 `main`。
不需要绕过规则或强制推送权限。若 `data` 规则要求 PR，需要你单独决定如何允许正常机器人快进提交，
不要直接关闭全部保护。参考 [GitHub 令牌说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)。

GitHub 账户集成和 CircleCI 的 `GH_PAT` 是两套凭据。GitHub Actions Secret 明文无法通过 API 读取，
需从你自己的凭据来源重新输入。不要把秘密写入 YAML、README、仓库文件或聊天。

## 3. CircleCI 项目及全部环境变量

1. 登录 [CircleCI](https://app.circleci.com/)，选择 Free，通过 **GitHub App** 连接仓库，
   尽量仅授权本仓库。不要启用付费自动充值。
2. 选择 `.circleci/config.yml`；Config branch、Checkout branch 均为 `main`。
   合并前可在迁移分支运行默认离线检查。
3. 在 Project Settings → Environment Variables 添加以下变量，名称区分大小写。
   不要把秘密放在 pipeline parameters。关闭向 fork PR 传递 secrets，只允许可信维护者修改配置。
4. 先不要创建启用状态的 Schedule，也不要开启两个 `ICOURSE_ENABLE_*` 开关。

### 必须手动配置

| 名称 | 完整运行是否必需 | 值/作用 |
| --- | --- | --- |
| `GH_REPOSITORY` | 建议显式设置 | `fengshuiling/Fudan_iCourse_Subscriber`；未设置时尝试 CircleCI 项目 owner/name |
| `GH_PAT` | 是 | 上述 Fine-grained PAT；绝不读取或回退到 `GITHUB_TOKEN` |
| `STUID` | 是 | 原数据库使用的学号，与原值完全一致 |
| `UISPSW` | 是 | 原数据库使用的 UIS 密码，与原值完全一致 |
| `COURSE_IDS` | 是 | 完整订阅列表，英文逗号分隔，例如 `35472,30251` |
| `SMTP_EMAIL` | 是 | 原发件 QQ 邮箱 |
| `SMTP_PASSWORD` | 是 | SMTP 授权码；旧格式解密也可能依赖原值 |
| `RECEIVER_EMAIL` | 是 | 摘要邮件接收地址 |
| `ICOURSE_ENABLE_RUN` | 确认后开启 | 初始 `false` 或留空；确认后设成精确的 `true` |
| `ICOURSE_ENABLE_DATA_WRITE` | 确认后开启 | 初始 `false` 或留空；确认允许写回后设成精确的 `true` |

### LLM 密钥与可选变量

| 名称 | 是否必需 | 说明 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | 至少一个 LLM key | ModelScope key；若历史库仍用旧密钥派生，需保留当时原值 |
| `DEEPSEEK_API_KEY` | 同上 | DeepSeek key |
| `GEMINI_API_KEY` | 同上 | Gemini key |
| `DASHSCOPE_BASE_URL` | 可选 | ModelScope 兼容端点；不填沿用 `MODEL_PROVIDERS` 默认值 |
| `DEEPSEEK_BASE_URL` | 可选 | DeepSeek 兼容端点；不填沿用默认值 |
| `GEMINI_BASE_URL` | 可选 | Gemini OpenAI 兼容端点；不填沿用默认值 |
| 自定义 provider 的 `api_key_env` / `base_url_env` | 使用自定义 provider 时 | 名称与 `src/runtime/config.py` 逐字一致，直接添加到 CircleCI 即可 |
| `CRAWL_TERM` | 可选，兼容旧部署 | 可保留原值；当前 main 自动发现学期，目录为空或每月 5/25 日刷新目录，迁移不改变该逻辑 |
| `StuId` / `UISPsw` | 兼容旧变量名 | 可替代全大写变量；若两组均设置但内容冲突，预检查停止 |

`SUBSCRIBED_COURSE_IDS` 无需手填：发布时自动从完整的 `COURSE_IDS` 设置，供原 PSA hot/cold 分片使用。
不要用临时单课列表替代完整订阅。SMTP 仍使用原 `smtp.qq.com:465`，主机和端口不是新增环境变量。

### 配置已提供的默认值

| 名称 | 默认值 | 作用 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 日志及目录刷新日期使用北京时间；调度器仍使用 UTC |
| `DATA_DIR` / `DB_PATH` | `data` / `data/icourse.db` | 安全适配脚本要求固定路径，勿在项目变量中覆盖 |
| `ASR_BACKEND` | `sensevoice`（原程序默认） | 更换 FireRed/Zipformer 前需修改安装脚本、缓存键及路径；目前会拒绝其他后端 |
| `ASR_MODEL_DIR` | `.cache/icourse-models/sensevoice` | CircleCI 模型目录；原程序仍兼容 `SENSEVOICE_MODEL_DIR`，此处优先用新名 |
| `SILERO_VAD_PATH` | `.cache/icourse-models/silero_vad.onnx` | VAD 路径 |
| `ASR_NUM_THREADS` | `2` | 配合 2 vCPU |
| `OCR_MAX_WORKERS` / `OCR_MAX_TARGET` | `1` / `1` | 限制 OCR 并发和内存 |
| `OCR_NUM_THREADS` | `1` | 显式限制 RapidOCR 内部 ONNX 线程；不设置时保持原行为 |
| `IMAGE_WORKERS` | `4` | 缩小图片下载池 |
| `VIDEO_DOWNLOAD_CONCURRENCY` | `1` | 同时只解码一段音频 |
| `OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS` | `1` | 限制数学库线程竞争 |
| `ICOURSE_MAX_LECTURES` | `2` | 每次最多两节待处理课次，按原枚举顺序；可调成正整数，不允许 0 无限运行 |
| `PYTHONUNBUFFERED` | `1` | 及时输出进度 |
| `PIP_DISABLE_PIP_VERSION_CHECK` | `1` | 减少无关网络检查 |

`ICOURSE_JOB_STARTED_AT`、`ICOURSE_DEADLINE_EPOCH` 由作业生成，不要手填。
项目/Context 变量可覆盖 job environment；覆盖并发值时必须重新检查资源使用。

## 4. 先做只读历史验证

配置 `GH_REPOSITORY`、`GH_PAT`、`STUID`、`UISPSW` 后，在 CircleCI 手动触发 `main` pipeline：

```json
{"verify_database": true, "run_course_check": false}
```

若历史库仍是旧格式，还需原 `DASHSCOPE_API_KEY`、`SMTP_PASSWORD`。
此路径只安装 PyCryptodome、拉取 `data`、解密并检查数据库，不登录复旦、不下载 ASR 模型、
不调用 LLM、不发邮件、不推送提交。两个启用开关继续保持 false。
成功输出 `Read-only verification completed`；令牌写权限、校园网络和邮件仍待正式验证。

## 5. 确认后设置每日北京时间 19:36

GitHub App 项目：Project Settings → Project Setup → 对应 pipeline → Schedule +：

| 配置 | 值 |
| --- | --- |
| Name | `icourse-daily-1936-beijing` |
| Config branch / Checkout branch | `main` / `main` |
| Schedule (UTC cron) | `36 11 * * *` |
| Parameters | `run_course_check=true`，`verify_database=false` |
| Actor | Scheduling system（受限 Context 需选有权限的 actor） |
| Status | 确认前 Disabled；确认后 Enabled |

UTC 11:36 对应北京时间 19:36。CircleCI 可能增加最多约 10 分钟的固定偏移，排队也可能延后，
不能保证精确到分钟。不要在 Runner 内 sleep 等待时间，这会浪费免费额度。
不要给 GitHub App 项目直接添加旧式 `workflows.triggers.schedule`；该 YAML 调度不支持此集成。
旧 GitHub OAuth 的 timetable 接口可能仅表达小时窗口，建议使用 GitHub App 的 cron 调度。
参考 [调度文档](https://circleci.com/docs/guides/orchestrate/schedule-triggers/)。

你确认抓取和写回后，将两个 `ICOURSE_ENABLE_*` 设为 `true`，再启用 Schedule。
手动完整执行同样在 `main` 设置 `run_course_check=true`。普通 push、`data` 提交、未设置参数的
API 调用不会完整抓取。不要启用自动失败重跑，避免邮件已发出而数据库尚未发布时重复发送。

## 6. 数据保护与恢复

1. 用 GH_PAT 创建独立临时 Git 仓库，读取固定 `data` 提交。分支不存在、网络/权限失败、
   没有可识别历史库均停止，不自动初始化空库。
2. 有 `icourse-index.enc` 就必须完整验证索引和引用分片：文件名、长度、SHA-256、解密、SQLite。
   损坏时不回退到旁边可能过期的旧单文件。只有全部成功后才生成运行用数据库。
3. 支持旧 `icourse.db.gz.enc` / `icourse.db.enc`：先试 v2 100000 次 PBKDF2，再按原逻辑试
   旧版 10000 次 PBKDF2。两种密钥均失败时不创建运行库、不执行 main、不写回。
4. 保持 `sha256("ICSv2:" + STUID + ":" + UISPSW)`、OpenSSL `Salted__` AES-256-CBC、gzip、
   原 hot/cold/meta 分片名称及确定性加密规则。迁移时不要顺便更换影响历史解密的凭据。
5. main 成功退出后，用 SQLite backup API 包含 WAL 中已提交更改；重新拉取最新历史库，
   调用原 `merge_db.merge` 和 `shard_database`。新分片重新解密重组并逐行比较五张表，
   避免分片漏掉孤立数据后仍发布。
6. 仅提交加密索引/分片及旧加密单文件的删除，保留 `data` 分支其他文件。提交以最新 `data`
   为父节点，使用普通快进 push，绝不 force-push。并发拒绝后最多重试三次，每次重读、重解密、
   重合并、重分片；任何读取失败立即停止。
7. CircleCI `serial-group` 串行化本项目完整运行，但不能锁住 GitHub 的其他写入者。切换时
   停用旧每日检查，勿同时运行未迁移的单次抓取、删除、手工重分片。
8. 明文、音频、历史快照不保存到 cache/workspace/artifacts。普通失败清理运行库；强制取消或
   Runner 回收时依赖临时容器销毁，失败作业不发布部分结果。

原合并策略保留：非空字段优先、成功后清零错误状态、PPT 已有行不覆盖。这不能解决通用语义冲突；
并发删除或两个运行修改同一课次需额外协调。`[skip ci]` 和分支过滤防止数据提交引发新抓取。
快进提交保留可恢复历史，长期仓库体积会增长；脚本不会自动压缩或清空历史。

遇到 STOP 先修复权限、凭据、缺失文件或网络，再只读验证。恢复旧快照前备份当前 `data`，
离线验证已知可解密提交，再单独人工审核恢复操作。没有“忽略解密错误继续写入”的开关。

## 7. CircleCI Free 资源评估（2026-10-21 查阅）

| 项目 | 选择与限制 |
| --- | --- |
| 计算资源 | 完整运行 Docker `medium`：2 vCPU / 4 GB RAM；离线/只读用 `small` |
| 免费额度 | 当前每月 30000 credits；按 medium 10 credits/min，31 天每天 55 分钟约 17050 credits，未含其他项目、重跑、验证及存储费用 |
| 单作业时长 | Free 约 1 小时；原 Actions 360 分钟不能照搬 |
| 预算 | 从 checkout 前计时，55 分钟内部预算减去已花时间、预留 5 分钟发布；main 提前 3 分钟停止开始新课次/目录。安装与读取后不足 5 分钟就不开始抓取 |
| 看门狗 | main 超预算终止进程组，禁止发布；15 分钟静默超时不能突破总时长 |
| 缓存 | `.venv` 按固定镜像/架构/requirements checksum 缓存；ASR 只保存固定版本 int8/VAD；15 天保留，不按每次提交生成新缓存 |
| OCR 模型 | 随 `rapidocr-onnxruntime` 安装，实际模型缓存由 `.venv` 覆盖；原 `~/.cache/rapidocr` 不是该包的默认模型位置，不再另存空的重复缓存 |
| 存储 | Free 当前包含 2 GB；新依赖/模型版本产生旧缓存时，在 Usage 中清理。缓存不作为数据库历史来源 |

不保证所有课程一天内处理完成。默认每天最多两节，积压跨天继续；每天新增量超过处理能力则积压增长。
长音频、大量 PPT、冷缓存安装、学期目录刷新、LLM 重试可能超预算。硬超时保留远端旧库，
当天工作可能需次日重做。邮件是外部副作用：发送成功后 push 失败或超时，下一次可能重复发送，
原程序没有事务式邮件 outbox。

首次正式运行检查 Resources 的峰值 CPU/内存、完整耗时及 Plan Usage 中 credits/缓存大小，再调整课次数。
不要默认升级规格、并行加载模型、安装 PDF/浏览器依赖或保存录屏。requirements 原来未锁版本，
本迁移保留该策略；未来冷缓存可能安装新版依赖，如需锁定应先在 Linux 3.12 Runner 实测。
参考：[资源配置](https://circleci.com/docs/reference/configuration-reference/)、
[价格与额度](https://circleci.com/pricing/)、[超时限制](https://support.circleci.com/hc/en-us/articles/360007188574)。

## 8. 前端不变后的待迁移功能

| 功能 | 当前状态与后续工作 |
| --- | --- |
| 已部署 Pages、浏览器解密、课程浏览 | 前端源文件和数据接口不变；继续读取原 `data` 路径 |
| `check.yml` 手动入口/旧检查按钮 | 只显示迁移说明；完整检查改在 CircleCI 触发，旧按钮不自动转发 |
| 单次抓取 | `single_run.yml` 仍依赖 Actions，未迁移，勿与 CircleCI 同时写库 |
| 删除课程/课次 | `delete_course.yml` 仍依赖 Actions；需单独迁移并共享串行队列，避免删除与 additive merge 冲突 |
| PDF/HTML/Markdown 导出及邮件 | `export.yml`、`scripts/export_course.py` 仍依赖 Actions；后续独立轻量 job，仅 PDF 才装 WeasyPrint/字体 |
| 前端部署 | `deploy-frontend.yml` 仍依赖 Actions/Pages OIDC；既有页面通常继续服务，新部署受 Actions 禁用影响，需另选部署方式 |
| 前端订阅编辑器 | 保存的是 **GitHub Actions 的 COURSE_IDS Secret**，不会修改 CircleCI！现阶段必须在 CircleCI 手动更新 `COURSE_IDS`，后续需安全后端桥接 |

不要把 CircleCI API token 嵌入静态前端，或扩大日常 GH_PAT 权限来伪装迁移这些功能。
GitHub 账户/仓库级 Actions 禁用与迁移独立，本改动不会自动解除 GitHub 的限制。
本次只读查询显示五个 workflow 的 `state` 均为 `active`，最近查询到的日常检查成功记录为
2026-10-14；这不能证明账户/执行器层面没有限制，也不能确定你看到的禁用提示的原因。

## 9. 验证和启用顺序

```bash
python -m pip install pycryptodome PyYAML
python -m compileall -q main.py src scripts
python -m unittest discover -s ci_tests -v
```

测试只用合成密钥/数据库与本地 bare Git，覆盖两种旧密钥和压缩格式、分片往返、错误密钥/缺失/损坏、
非法索引、WAL 备份、保留远端独有行、并发拒绝重合并、发布时解密失败禁止 push、默认关闭的执行参数。

本次已通过 CircleCI 官方 CLI 的 `circleci config validate .circleci/config.yml` 验证，
并在本地 Python 3.12.14 通过 17 项离线测试和 Python/安装脚本语法检查。
真实历史解密、PAT 写权限、GitHub App checkout/调度、Linux 模型启动、WebVPN、LLM 模型可用性、
SMTP、峰值资源需要后续验证。本次尚未执行真实 CircleCI 任务。

启用顺序：审阅合并 → 配置变量 → 只读验证 → 你确认抓取和写回 → 开启两个开关 → 启用 Schedule。
