# CI/CD 计划：让每个新数据引出的长尾问题都留下一条自动检查

写于 2026-11-16。状态：**计划，尚未实施**。进度在 `progress/plan.json` 的 P8 阶段。
同一时间另一个 session（"Google Batch 集成讨论"）在把 Nextflow 接到 Google Batch，第 7 节写了两边怎么分工、不撞文件。

## 0. 结论先说

1. **CI 的目标是"同一个坑只踩一次"，不只是"代码能跑"。** 每个新 study 带来的长尾问题（E1–E8、`studies/LESSONS.md`）都要变成一个小测试样本和一条测试，以后每次改代码都自动回归。
2. **把"软件对不对"和"科学上对不对"分开测。** 软件层：单元测试、数据库迁移、迷你端到端。科学层：mock community 已知答案、复现论文结论的方向、跨 study 的 QC 指标漂移。
3. **CI 里不联网、不花钱、不调用 LLM。** ENA/NCBI 的响应录下来回放，LLM 读存好的原始响应。联网和真 LLM 的评估只在手动触发时跑。
4. **CD 指的是"可追溯地发布 pipeline 版本"**：打 tag 的代码版本、带 commit sha 的镜像、每个结果都能查到是哪个版本算出来的。它和 Google Batch 的镜像发布是同一件事，等那边落地后再接（第 6 节）。
5. **2026-11-16 已定：GitHub repo 先私有，以后公开。** 因此第一次提交起，整个 git 历史都要能公开：不放邮箱、billing ID、密钥（`docs/gcp-handoff.md` 留在本地，`paper-discover2sra` 暂时保持独立的私有 repo）。

## 1. 前提和现状（2026-11-16 实际查到的）

| 项 | 现状 | 对 CI 的影响 |
|---|---|---|
| git | 主目录**不是 git 仓库**；`paper-discover2sra/` 是单独的 repo（github.com/chjp/paper-discover2sra） | 没有 repo 就没有 GitHub Actions。要决定 `paper-discover2sra` 用 submodule、并进主 repo，还是保持独立并各自 CI |
| GitHub | `gh` 已登录（账号 chjp） | 建 repo 一条命令，但要 owner 确认公开/私有 |
| 测试 | `pipeline/tests` 39 个通过（8.9 s）；`ml/tests` 7 通过、5 跳过（80 s，跳过的应该是需要数据库的） | 已有基础。跳过的 5 个在 CI 里用 Postgres 服务容器跑起来 |
| 依赖 | ~~没有 lock 文件，测试靠 `uv run --with ...` 临时拼~~ **2026-11-16 已完成（8.2）**：`pyproject.toml` + `uv.lock`（dev / db / ml / dashboard 四组），`scripts/check.sh` 约 20 秒跑完全部检查 | 本地和 CI 用同一套固定版本、同一个入口 |
| 工具版本 | `pipeline/versions.env` 已固定 Nextflow、ampliseq、fetchngs、SILVA | 好。CI 读同一个文件，不另写版本 |
| 网络 | `preflight.py` 直接从 ENA 流式读 reads；`sra_source.py` 查 NCBI SDL | 要加"回放模式"，测试里不联网（已有 `ncbi_sdl_SRR578443.json` 这类样本，可以照这个思路扩展） |
| 溯源 | `pipeline_run.json` 记了 ampliseq 的 commit，**没记本仓库自己代码的 commit** | CD 要补：每次运行写入 wrapper 代码的 git sha 和是否有未提交改动 |
| 大文件 | `phase0/data` 29 MB、`paper-discover2sra/runs` 98 MB；`.gitignore` 已排除 `phase0/`、`progress/`、`studies/*/sources/`、`.env`、tfstate | 首次提交前跑一次 secret 扫描（gitleaks），再看一遍要不要提交 `studies/*/work/`（agent 写的脚本，每个 32–64 KB） |

GitHub Actions 的额度（**动手前核对 GitHub 官网**）：公开 repo 的标准 runner 免费，Linux runner 约 4 vCPU / 16 GB；私有 repo 免费账号每月约 2000 分钟，Linux runner 约 2 vCPU / 7 GB，macOS 分钟按 10 倍计。这一点会影响下面的迷你端到端能不能用完整 SILVA（TAXONOMY 一步实测峰值 6.2 GB）。

**owner 的决定（2026-11-16）：** 先私有，以后公开。私有 repo 的 runner 小（约 2 vCPU / 7 GB），每月约 2000 免费分钟，所以 L3 改为每周和改到 pipeline 时跑，不每晚跑，并且用裁剪参考库。License 等公开前再定。

本地 git 已初始化，第一次提交前扫过一遍：detect-secrets 没有发现密钥（命中的都是 md5、sha256 和路径）；带个人邮箱和 billing ID 的 `docs/gcp-handoff.md` 已经加进 `.gitignore`，`plan.json` 里的 billing ID 已删掉。文件里还留有 Mac 的用户目录路径（`/Users/...`，在 `log.jsonl` 等文件里），不算敏感信息。

## 2. 测试分层

越往下越慢、越接近真实，跑得越少。

| 层 | 测什么 | 例子 | 何时跑 | 预计耗时（估计） |
|---|---|---|---|---|
| L0 静态检查 | 格式、语法、配置能否解析 | ruff；`python -m py_compile`；`studies/*/protocol.json`、`sample_sheet.tsv` 按 schema 校验；`shellcheck infra/*.sh`；`terraform fmt -check` + `terraform validate`（`-backend=false`，不碰 state 和凭据）；`nextflow config` 解析每个 profile | 每次 push / PR | < 2 min |
| L1 单元测试 | 函数级逻辑 | 现有 46 个；数据库测试连 Postgres 服务容器 | 每次 push / PR | 2–3 min |
| L1b 数据库迁移 | 迁移脚本从零能建库，重复跑不出错 | 新 Postgres 上依次跑 `db/migrate.py` 001–010，再跑一遍确认幂等；`db/queries/overview.sql` 能执行 | 每次 push / PR | 1 min |
| L2 长尾回归 | 每个踩过的坑一个最小样本 | 见第 3 节 | 每次 push / PR | 1–2 min |
| L3 迷你端到端 | 整条 pipeline 能串起来 | 两个小数据集跑 `run_study.py --from preflight --no-db`，Docker profile；检查完整性、QC 结论、关键指标快照 | 改到 `pipeline/` 的 PR、每周（私有期间）、手动 | 15–30 min（估计，含拉镜像） |
| L4 科学验证 | 结果和已知答案一致 | mock community；论文结论方向；LOSO AUC 不明显变差 | 版本升级（ampliseq、SILVA、参数）时、每周、手动 | 30–60 min（估计） |
| L5 数据 CI | 新 study 进来时先做体检 | 见第 5 节 | 新增或修改 `studies/<PMID>/` 的 PR | 几分钟 |
| L6 agent 评估 | LLM 抽取和找标签的准确率 | 见第 5.3 节 | 回放模式每次 PR；真调用只在手动触发 | 回放 < 1 min |

### 2.1 迷你端到端（L3）怎么做小

- **数据**：从已跑完的 study 里各抽几个 run、每个 run 抽 2000 条 reads，做成两个固定样本：
  - `mini_pe`：35756051 的 4 个 run（MiSeq 2×301，引物在 reads 里，需要显式 trunclen）；
  - `mini_454`：23013615 的 4 个 run（454 单端，87 % 反向，固定截断长度）。
  这两个刚好覆盖 E3、E4、E6 和反向 reads 的问题。几 MB，可以直接提交，或放 GitHub Release 附件。
- **参考库**：完整 SILVA 337 MB，而且 TAXONOMY 一步峰值 6.2 GB，私有 repo 的 runner 可能装不下。CI 用一个**裁剪过的参考库**：只保留样本里出现的属，外加一些干扰序列。这一层只验证流程串得起来，不验证分类准不准，分类准确性交给 L4。
- **跑法**：Linux runner 上 `NF_PROFILE=docker`。nf-core 镜像是 amd64，在 GitHub 的 Linux runner 上能直接跑，正好补上 Mac 上用不了 Docker 的缺口。Nextflow 的 `work/` 用 `actions/cache` 缓存，镜像按 `versions.env` 的 hash 缓存。
- **检查**：`collect.py` 的 integrity 检查全通过；QC 结论和预期一致（`mini_pe` 预计是 `review`，因为本来就有一个低保留的 run，这正好测试 gate 能不能报出原因）；关键指标和快照比较，**带容差**（见 2.2）。
- **跨平台**：`studies/35756051/pipeline_qc_subset6_gce.json` 已经有 Mac（conda）和 GCE（Docker）的对比。CI 的 Docker 结果再和这两份比一次，E1 要求的"两个平台结果核对一次"就自动化了。

### 2.2 快照怎么比（输出不完全确定）

- 精确比较：样本数、run 数、ASV md5 集合的 Jaccard ≥ 0.95、每个 run 的 abundance 合计 = DADA2 non-chimeric reads。
- 容差比较：`n_asv` ±5 %，`median_merge_rate` ±0.02，前 8 个属的占比各 ±0.02，`share_reads_plausible_length` ≥ 快照值 −0.01。
- 改动确实会改变结果时（升级 ampliseq 或参考库），在 PR 里用一条命令更新快照，**diff 作为 review 的一部分**：哪些指标变了、变了多少。

## 3. 长尾问题 → 回归测试（L2，这个计划的核心）

先把 `docs/experience-log.md`、`studies/LESSONS.md` 和 `plan.json` 的 decisions 整理成 `docs/failure-taxonomy.md`：每类一行，写明检测方式和对应的测试。初稿如下：

| 类别 | 来源 | 样本（`pipeline/tests/fixtures/`） | 测试要保证的事 | 现状 |
|---|---|---|---|---|
| 质量分丢失 | E3 | 几条只有 2 种质量值的 FASTQ + 录下的 NCBI SDL 响应 | preflight 选 `ncbi_sra_normalized`；SDL 的 reads 和 ENA 不一致时给 `needs_review` | 部分（`test_fetch_fallback`、SDL 样本已有） |
| 454 截断 | E4 | 长度参差的 454 reads | `trunclenf` = 75 % reads 达到的长度，并以中位质量 < 25 的位置为上限 | 待确认覆盖程度 |
| addSpecies 内存 | E5 | 不需要 | `n` 随内存上限变化（8 GB → 70） | 已有 `test_resources.py` |
| `-resume` 缓存失效 | E6 | 不需要 | 配置里 `process.cache = 'lenient'` 一直在；在 L3 里跑两次，第二次 cached 任务数 = 总数 | 新增 |
| macOS 没有内存记录 | E7 | 录下的 trace + ps 采样 | 采样和 trace 的 join 正确 | 已有 `test_resource_monitor.py` |
| 反向 reads | 23013615 | 以反向引物开头的单端 reads | 交换引物、`dada_taxonomy_rc`，collect 把 ASV 翻回正向 | 待确认 |
| 双端重叠不够 | 35756051 | 质量曲线让自动截断只剩 17 nt 重叠 | 给出显式 `trunclenf/r`，保证重叠 ≥ 32 nt | 待确认 |
| 论文没写引物 | 37661264 | 带 5' 端被截掉一截的 341F 的 reads | 引物目录匹配 ≥ 80 %，标为 `inferred` | 待确认 |
| 引物比例在中间 | 规则 | 30 % reads 带引物 | `needs_review`，并写出原因 | 新增 |
| md5 校验失败 | fetchngs | `fetchngs_md5_failure.txt` | 失败的 run 保留 FASTQ，cleanup 不删 | 已有 |
| 元数据：标签藏在各处 | LESSONS 1–5 | 5 种标签来源各一个最小 XML/表格 | 解析器能拿到自定义 BioSample 属性（LESSONS 说漏过 2 个 study） | 新增 |
| 元数据：非 16S 的 run 混在一起 | LESSONS | 同一 BioProject 里混有 WGS 的 runinfo | `is_16s` 标记正确，prepare 跳过 | 待确认 |
| 计数对不上 | LESSONS | 论文写的计数 vs sample sheet | validator 报出具体哪一组差几个，不静默通过 | 新增 |
| 单个慢步骤 | decisions 2026-11-16 | 不需要 | 新 study 的 `skip_dada_addspecies = true` 一直保留 | 新增（防止有人改回去） |

**以后的规矩**（写进 CLAUDE.md）：在 experience log 记一条新问题时，同一个 PR 里必须加一个回归测试，或者写明为什么测不了。CI 可以检查这一点：experience log 的条目数增加了，`tests/` 却没有改动，就给 PR 留一条提醒。

**怎样不联网**：给 `preflight.stream_reads` 和 `sra_source` 的 HTTP 调用加一层很薄的"录制/回放"。测试用环境变量 `AGENTIC16S_HTTP=replay`，从 `fixtures/http/` 读录好的响应；真跑时不受影响。这样第一次遇到怪数据时，顺手把响应录下来就是一个新样本。

## 4. 科学验证（L4）

### 4.1 Mock community（已知答案）

- 从 SRA 选一个 ZymoBIOMICS 微生物群落标准品（D6300 或 D6305/D6306）的公开 16S 数据集，**最好是 V4 和 V3-V4 各一个**，和真实 study 的区域对得上。具体 accession 还没选，要查过再定。
- 标准品有 8 种细菌：Pseudomonas、Escherichia、Salmonella、Lactobacillus（新分类叫 Limosilactobacillus）、Enterococcus、Staphylococcus、Listeria、Bacillus。按 16S 拷贝数校正后的理论比例要以厂家说明书为准。
- 指标：8 个属的召回数（目标 8/8）；不该出现的属的 reads 占比（例如 < 1 %）；和理论组成的 Bray-Curtis 距离；每个属的观测/理论比值。
- 这里本身就有长尾问题可讲：V4 区分不开 Escherichia 和 Salmonella（SILVA 里还有 `Escherichia-Shigella` 这种合并名称），Lactobacillus 改过名。属名映射要写成表，不能写死在代码里。
- 什么时候跑：升级 ampliseq、SILVA、DADA2 参数，或者改 preflight 的决策规则时。比较前后两次的结果，作为升级 PR 的证据。

### 4.2 复现论文结论的方向

`pipeline_qc.json` 已经有 `sanity_faecalibacterium_mean_share_by_group`。把它扩成每个 study 一个"预期方向"文件（`studies/<PMID>/expected_findings.json`）：论文报告的 1–3 个主要差异菌属和方向，再附上原文引语作为证据。CI 检查方向是否一致。不一致不一定是 bug，但必须有人看过。

### 4.3 ML 不能悄悄变差

`ml/loso.py` 的 LOSO AUC 和置换基线存成快照（`ml/results/` 已有）。数据或代码改动之后，AUC 下降超过预设阈值（例如 0.03），或者跌到置换基线的 95 分位以下，就报告出来。这也是防数据泄漏的最后一道检查：如果 AUC 突然大涨，同样要报告。

## 5. 数据 CI：新 study 进来时（L5）

这是你说的"不断进新数据"的那部分：**每个新 study 当成一个 PR 来 review**。

### 5.1 新增 `studies/<PMID>/` 时自动跑

1. 文件齐全：`protocol.json`、`sample_sheet.tsv`、`validation.json`、`log.jsonl`。
2. schema 校验：区域、引物、平台的取值在允许范围内；每个 run 都有 case/control 或明确的缺失原因；标签带来源。
3. 计数核对：`validation.json` 里论文写的计数和 sheet 一致；不一致必须有解释。
4. preflight 回放：用 PR 里附带录好的 reads 样本跑 preflight，输出 `params.json` 的 diff，让人看到这个 study 触发了哪些非默认决策。
5. 和已有 study 比较（漂移检测）：这个 study 的 QC 指标（保留率、merge rate、嵌合体比例、未分类比例、alpha 多样性中位数）放到已有 study 的分布里看，用 robust z-score（中位数/MAD）标出离群值。离群不阻止合并，只要求写一句解释。

### 5.2 要记录的"首次成功率"

每个新 study 记一行：第一次跑是否一路通过（不需要改代码）、在哪个阶段失败、属于第 3 节哪一类、修了多久。存到 `docs/onboarding-metrics.csv`，由 `log.jsonl` 自动汇总。这是面试时能拿出来的数字：N 个 study、M 类问题、首次成功率从 X% 到 Y%。

### 5.3 Agent / LLM 的评估（L6）

- 金标准：5 个人工核对过的 study（`studies/LESSONS.md` 那张表）的计数、区域、引物、标签来源。
- 回放模式：读存好的原始 LLM 响应，重新跑解析和校验代码。确定性、免费，每次 PR 都跑。改 prompt 或解析代码时能立刻看出有没有变差。
- 真调用模式：手动触发，用 GitHub secret 里的 API key，按模型分别记准确率、token 成本、耗时。**要 owner 决定放不放 key、每月预算多少**；在此之前不做。

## 6. CD：发布什么、怎么追溯

| 发布物 | 做法 | 依赖 |
|---|---|---|
| 代码版本 | `main` 受保护，合并前 L0–L2 必须通过；发布打 tag `v0.x.y` | git repo |
| 溯源 | `run_study.py` 把本仓库的 git sha 和 dirty 标记写进 `pipeline_run.json`，入库到 `bio.pipeline_run` | 要改 `run_study.py`，等 Batch session 改完再动（第 7 节） |
| 运行镜像 | 一个包含本仓库 `pipeline/bin` 和 Python 依赖的镜像，tag = commit sha，推到 GHCR 或 Artifact Registry；Google Batch 的作业固定用某个 sha | Batch session 决定镜像怎么用 |
| 云上认证 | GitHub Actions → GCP 用 Workload Identity Federation，**不建 service account key**（`docs/gcp-handoff.md` 的规矩）；只给推镜像的权限 | owner 执行 `terraform apply` |
| Terraform | CI 只做 `fmt` 和 `validate`。**不在 CI 里 plan 或 apply**：state 在本地，而且 apply 每次都要 owner 确认 | 无 |
| 数据库迁移 | 迁移在 CI 里对空库和"上一个版本的库"各跑一次；生产库（本地 PostgreSQL）仍然手动 `db/migrate.py` | 无 |
| Dashboard | Phase 6.3 的 Docker Compose 做完后，CI 构建镜像并跑一次健康检查；不部署到公网 | 6.3 |

## 7. 和 Google Batch session 的分工

两个 session 在同一个文件夹里，没有 git 也没有 worktree，所以**靠划分文件避免互相覆盖**：

| 区域 | 谁改 |
|---|---|
| `infra/*.tf`、`infra/*.sh`、`pipeline/conf/`、`pipeline/versions.env`、`docs/gcp-migration.md` | Google Batch session |
| `pipeline/bin/run_study.py` | Google Batch session 先改；CI 需要的改动（git sha、HTTP 回放开关）等它完成后再做 |
| `.github/`、`pyproject.toml`、`uv.lock`、`pipeline/tests/`、`ml/tests/`、`docs/ci-cd-plan.md`、`docs/failure-taxonomy.md`、新的校验脚本 | 本 session（CI/CD） |
| `progress/plan.json`、`docs/experience-log.md` | 两边都会改：每次改之前重新读最新版本，只追加自己的条目，不整体覆盖 |

交汇点：Batch 落地后，CI 加上它的检查。`nextflow config -profile google_batch`（或那边起的名字）要能解析；Terraform 新增的资源过 `validate`；镜像 tag 规则两边统一。

## 8. 分步实施

工作量都是估计。

| 步骤 | 内容 | 谁 | 工作量 | 依赖 |
|---|---|---|---|---|
| 8.0 | 本计划 | agent | 完成 | |
| 8.1 | 建 git 和 GitHub repo（**私有，以后公开**）；首次提交前做 secret 扫描。本地 git 已初始化并扫描完，等 owner 批准首次提交和 push | owner + agent | 进行中 | owner 批准 |
| 8.2 | `pyproject.toml` + `uv.lock`；一个统一入口 `scripts/check.sh`（本地和 CI 跑同一个）。**已完成 2026-11-16** | agent | 完成 | 无 |
| 8.3 | L0 + L1 + L1b 的 GitHub workflow（含 Postgres 服务容器）。`.github/workflows/ci.yml` 已写好，在本地验证过，等 push 后第一次在 GitHub 上运行 | agent | 进行中 | 8.1 |
| 8.4 | `docs/failure-taxonomy.md` + HTTP 回放层 + 第 3 节表里的新测试 | agent | 1 天 | 8.2；回放开关要等 Batch 改完 `run_study.py` |
| 8.5 | 两个迷你数据集 + 裁剪参考库 + L3 workflow（每晚、改到 pipeline 时、手动） | agent | 1 天 | 8.3；需要在 runner 上实测耗时和内存 |
| 8.6 | Mock community：选数据集、属名映射、指标、L4 workflow | agent + owner | 1 天 | 8.5；owner 确认用哪个数据集 |
| 8.7 | 数据 CI（第 5.1 节）和首次成功率记录（第 5.2 节） | agent | 1 天 | 8.3 |
| 8.8 | Agent 评估回放（第 5.3 节） | agent | 0.5–1 天 | LLM 原始响应要已经存下来 |
| 8.9 | CD：git sha 溯源、镜像、WIF | agent + owner | 1 天 | Batch session 完成；owner apply |

推荐顺序：8.2 → 8.1 → 8.3 → 8.4 → 8.5 → 8.7 → 8.6 → 8.9 → 8.8。8.2 和 8.4 的文档部分现在就能做，不受 git 和 Batch 的影响。

## 9. 面试时怎么讲

一句话：**"每接一个新 study，就把它暴露的问题变成一条自动检查。"** 能拿出来的东西：
- 故障分类表：每类问题、怎么发现、对应哪个测试；
- CI 记录：每个 PR 过了哪些层；
- 首次成功率曲线：随 study 数增加的变化；
- mock community 在每次版本升级前后的对比；
- 一次具体的故事，比如 E6：`-resume` 静默重跑，怎么发现，怎么修，现在由哪条测试守着。
