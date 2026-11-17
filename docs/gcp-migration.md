# 迁移到 Google Cloud 的考虑和 Terraform 方案

写于 2026-11-15。前提：订阅 Google AI Pro（$19.99/月），用它附带的 Colab 计算单元和 Google Cloud credit。
文中的价格是凭记忆的 **大致值**，动手前用 [GCP Pricing Calculator](https://cloud.google.com/products/calculator) 和 Billing 页面核对。

## 0. 结论先说

1. **不要整体搬走 Mac mini，改成混合式。** LLM 策展（Claude Code / `claude -p`）、PostgreSQL、Streamlit 留在 Mac 上；
   只把 **Nextflow 的生信计算**（fetch → ampliseq → collect）放到 GCP 上的一台 **普通（on-demand）VM** 上，平时关机，用的时候才开，跑完就停。
2. **Colab 不适合跑主流程。** 它适合 Phase 6 的 ML 笔记本和演示，不适合 Nextflow + conda/Docker + 长时间任务。
3. **Pro 的 Cloud credit 已核实（2026-11-15，见 1.1）：每月 $10，每笔有效期 12 个月，可以累积**，一年最多攒到约 $120。
   **Spot VM 在 Google 的标准 credit 排除清单里**，所以想用 credit 付钱，就要用普通 VM，不能用 Spot。
   $10 大约够 `e2-standard-8` 普通 VM 跑 35 小时左右，或者 Terraform 默认的 `e2-standard-2`（smoke test 用）跑 150 小时左右。数据盘一直挂着的话（每月 $10–20），会把 credit 吃掉一大半，所以两批任务之间要把盘删掉。
4. **Terraform 只管理少量资源**：项目 API、一个 VM（默认关机）、一块数据盘、一个 GCS bucket、防火墙，再加预算告警。
   这个规模刚好可以放进作品集，说明 "infra as code + Nextflow on cloud"。

## 1. AI Pro 里和这个项目有关的部分

| 权益 | 对本项目有没有用 | 说明 |
|---|---|---|
| Google Cloud credit（Developer Program premium） | **有用，但额度小** | 拿来付普通 VM、磁盘和 GCS Standard 存储的钱（Spot 不行，见 1.1）。不要用来开 Cloud SQL 或常驻 VM |
| Colab 200 compute units/月 | 部分有用 | CPU 运行时每小时耗的 CU 很少，GPU（尤其 A100）耗得很快。DADA2 和 Cutadapt 都只用 CPU，**GPU 对本项目没用** |
| 5 TB Drive | 有用（备份） | 存 `tables/`、MultiQC 报告、数据库 dump。不要拿 Drive 当 Nextflow 的工作目录，挂载后又慢又不稳 |
| Gemini / AI Studio 额度 | 可选 | proj.md 设计成 provider-agnostic，可以用 Gemini 在 gold set 上做模型对比，算一个加分项 |
| Antigravity、Gemini in Android Studio 等其他权益 | 无关 | |

### 1.1 Cloud credit 核实结果（2026-11-15）

| 问题 | 结论 | 来源 |
|---|---|---|
| 每月多少 | **$10**（Ultra 5x 是 $40，Ultra 20x 是 $100） | [Benefits FAQ](https://developers.google.com/profile/help/benefits)、[Google One 帮助](https://support.google.com/googleone/answer/14534406?hl=en) |
| 会过期吗 | 每笔 credit **从发放日起 12 个月内有效**，按月累积，最早到期的先用 | Benefits FAQ、[AI Studio 领取说明](https://blog.google/innovation-and-ai/technology/developers-tools/google-cloud-credits-gdp-ai-studio/) |
| 什么时候发 | 按你第一次领取那天的月度周年日发，不是每月 1 号 | Benefits FAQ |
| 怎么领 | 订阅 AI Pro → 建 Developer Program profile → 在 [Benefits 页面](https://developers.google.com/program/my-benefits) 选一个 Cloud Billing account；也可以拿 promo code 去 `console.cloud.google.com/billing/redeem` 兑换。勾选 "Always use this billing account" 以后，每月自动入账 | Benefits FAQ |
| 必须有 billing account 吗 | 必须有。credit 只能绑定一个 billing account。需要你本人有 `billing.accounts.redeemPromotion` 权限（账号 owner 默认就有） | Benefits FAQ |
| 能用在哪些产品上 | "any eligible Google Cloud or Google Maps Platform product"，标准排除项见 [Discount Exclusions](https://cloud.google.com/skus/exclusions) | Benefits FAQ |
| **排除项里和本项目相关的** | **"Preemptible and Spot offerings"**；Archival/Coldline 存储；GKE；Marketplace；Support；"Select specialized VMs"（某些内存优化、计算优化机型，以及部分 GPU/TPU） | Discount Exclusions（最后修改于 2024-10-24） |
| 只能本人用 | 只有订阅主账号能用，家庭组成员不能共享 | AI Studio 领取说明 |

对本项目的影响：
- **worker 用普通 VM**（`e2`、`n2d` 都是通用型，不在排除项里），Spot 只在你愿意用信用卡付钱时才开。
- GCS bucket 用 **Standard** 存储类别，不要配 lifecycle 转到 Coldline 或 Archive。
- Phase 7 如果改用 Google Batch：Batch 本身就是开 Compute Engine VM，可以用 credit，但同样不能选 Spot。不要用 GKE。
- **还没核实的**：你账号里的 credit 是否真的可以花在 Compute Engine 上。领取以后到 Console → Billing → Credits 看这笔 credit 的 "Scope"（适用的服务），以那里为准。
- 你的 Benefits 页面我在内置浏览器里打开时显示 "Join the program"，说明浏览器没有登录，或者你还没加入，所以看不到你自己的余额。

## 2. Colab：能用在哪里，不能用在哪里

**不适合跑 pipeline：**
- 会话有时长上限，空闲也会断开，断开后本地盘清空。每次重连都要重装 Nextflow、Java 和约 5 GB 的 conda 环境（或拉 Docker 镜像，而 Colab 里一般不能跑 Docker）。
- 没有常驻的 PostgreSQL，`load_bio.py` 写进去的数据会随会话消失。
- 一个 study 的 ampliseq 可能跑好几个小时，中途断开就前功尽弃，`-resume` 依赖的 `work/` 也没了。

**适合：**
- **Phase 6 ML**：把分析视图导出成 parquet，放到 Drive，在 Colab 里做 leave-one-study-out 交叉验证，还能直接附一个可运行的 notebook 链接，给面试官看。
- **演示**：写一个"拿一个 study 的 `tables/` 画几张图"的 notebook，README 里放 "Open in Colab" 按钮。

## 3. 从 Mac mini 迁到 GCE 要考虑的事

### 3.1 能解决的问题
- **amd64 Linux 可以直接用 nf-core 的 Docker 容器。** 现在 `conf/ampliseq_osx_arm64.config` 里的 osx-arm64 补丁（python 3.8.20 替换、跳过 phyloseq/TSE/report）
  在 Linux + Docker 下大多不再需要，复现性也更好：容器 digest 会进 `pipeline_run.json`。`versions.env` 已经预留了 `NF_PROFILE=docker`。
- **内存不再卡在 8 GB**（16 GB 的一半）。大 study 可以开 32 GB 甚至更多，不会影响 Mac 上别的工作。
- **下载更快**：ENA 下载属于 ingress，不收费。NCBI 的 SRA 数据在 GCS 上也有公开镜像（SRA in the Cloud），
  23013615 需要的完整 `.sra` 可以从 GCS 拿，不一定走 AWS。具体 bucket 和所在区域要先查 NCBI 文档。

### 3.2 需要改的地方
| 项目 | 现在（Mac） | 迁移后（GCE） |
|---|---|---|
| `NF_PROFILE` | `mamba` | `docker` |
| Nextflow 配置 | `conf/local_mac.config` | 新增 `conf/gce.config`（`docker.enabled`、`cleanup = true` 照旧） |
| `DATA_DIR` | `~/agentic16s-data` | `/mnt/data`（独立的持久盘，删 VM 不会丢） |
| `resources.config` | 内存一半、CPU 减 2 | `run_study.py` 照样按机器自动生成；VM 上没有桌面应用，可以放宽到 80–90 % |
| 结果回传 | 本地 | 用 `gcloud storage rsync /mnt/data/pipeline gs://<bucket>/pipeline` 推上去，Mac 再拉回来后跑 `load_bio.py` |
| 秘钥 | `.env` | 生信步骤不需要 LLM 秘钥。如果以后要在云上跑 agent，`OPENAI_API_KEY` 放进 Secret Manager，**不要**写进 Terraform 或 startup script |

### 3.3 数据库放哪
- **推荐：留在 Mac 上。** VM 只产出 `tables/`，同步回来以后在本地 `load_bio.py`。零额外成本，也不用在公网上暴露数据库。
- 备选 A：VM 上用 Docker 跑 Postgres，数据放在数据盘上。缺点是 VM 关机后 dashboard 也跟着停。
- 备选 B：Cloud SQL。最小的实例每月也要 $10 以上，会吃光 Pro 的 credit，**现阶段不推荐**。
- Phase 6.3 的 Docker Compose 做好以后，"Postgres + dashboard"可以整体部署到一台 e2-small（每月约 $12），等真需要公开 demo 时再加。

### 3.4 普通 VM 还是 Spot VM
- **默认用普通 VM**，因为 credit 不能付 Spot（见 1.1）。按 credit 算，普通 VM 反而是"免费"的那一个。
- Spot 比普通 VM 便宜 60–90 %，但随时可能被回收，而且只能用信用卡付钱。等 credit 不够用、需要大规模跑的时候再打开（Terraform 变量 `use_spot = true`）。
  被回收时设 `instance_termination_action = "STOP"`，只关机，磁盘还在。之后用 `run_study.py <PMID> --from ampliseq` 和 Nextflow 的 `-resume` 接着跑。
- Claude Code 的会话不要放在 VM 上跑。

### 3.5 Claude Code / agent 部分
- `paper-discover2sra` 用 `claude -p`，依赖你本机的登录状态。把它搬到 VM 上，就要在 VM 上登录或者配置 API key，得不偿失。**agent 步骤留在 Mac 上。**
- 以后 Phase 5 的 LangGraph 要做成服务时，再考虑 Cloud Run job 和 Secret Manager。

### 3.6 成本防护（最重要）
- 预算告警（`google_billing_budget`）**只会发提醒，不会自动停机**。真正兜底靠两点：VM 默认处于关机状态，startup script 里加一个"空闲 N 小时自动 `shutdown`"。
- 可能偷偷花钱的东西：常驻的外网 IPv4（运行时大约 $3.6/月）、Cloud NAT（每月约 $30 起，**不要开**）、快照、没删的磁盘、跨区域的 egress。
- 从 GCS 下载到本地 Mac 属于 egress，大约 $0.12/GB。`tables/` 很小，不要把 FASTQ 拉回本地。

## 4. 机型推荐

区域默认选 `us-central1`。如果要大量用 NCBI 的 GCS 镜像，就选和镜像 bucket 相同的区域。下面的价格是 us-central1 的大致值：

| 用途 | 机型 | vCPU / 内存 | 普通 $/h（credit 可付） | $10 credit 能跑 | Spot $/h（只能刷卡） | 什么时候用 |
|---|---|---|---|---|---|---|
| **全量跑的 worker** | `e2-standard-8` | 8 / 32 GB | ~0.27 | ~37 h | ~0.08 | 绝大多数 study，DADA2 一次跑一个 study |
| **Terraform 默认 / 最便宜的冒烟测试** | `e2-standard-2` | 2 / 8 GB | ~0.067 | ~150 h | ~0.02 | `--subset 6`；8 GB 和 Mac 上给 pipeline 的上限一样 |
| 冒烟测试，快一点 | `e2-standard-4` | 4 / 16 GB | ~0.13 | ~75 h | ~0.04 | 内存和 Mac mini 一样；`--subset 6` 验证容器 profile |
| 不推荐 | `e2-medium` | 2 共享 / 4 GB | ~0.034 | ~300 h | — | 4 GB 装 SILVA 分类有 OOM 风险，只适合装环境、试 startup script |
| 内存不够时 | `n2d-highmem-8` | 8 / 64 GB | ~0.45 | ~22 h | ~0.10 | DADA2 `learnErrors`/合并样本时 OOM |
| 大 study / 扩容（Phase 7） | `n2d-standard-16` | 16 / 64 GB | ~0.68 | ~15 h | ~0.15 | 上千个 run 的 study，或者一次并行好几个 study |
| 以后常驻 dashboard | `e2-small` | 2 共享 / 2 GB | ~0.017 | 一整个月 ~$12 | 不用 Spot | Phase 6.3 之后才需要 |

不要选 `c2`/`c3`/`m`-系列：部分计算优化和内存优化机型在 credit 排除项里。

磁盘：
- 启动盘：`pd-balanced` 30 GB（约 $3/月），Ubuntu 24.04 LTS。
- 数据盘：`pd-balanced` 默认 **100 GB**（约 $10/月，**哪怕 VM 关机也照样收费**）。FASTQ 在 integrity 通过后会被删掉，
  100 GB 够一个中等 study 的 FASTQ 加上 `work/`；碰到大 study 再临时调到 200 GB。
- **两批任务之间，删掉 VM 和数据盘**（`terraform destroy -target=...`，见 5.4），结果早已推到 GCS 上。conda/Docker 镜像下次重新拉，十几分钟就够了。
- 不需要 Local SSD 或 GPU。

粗算：一个中等大小的 study（几百个 run）在 `e2-standard-8` 上跑 3–6 小时，约 $1–1.6。按 $10/月算，每个月可以跑 5–8 个这样的 study，
credit 还能累积一年。**磁盘一直挂着才是最大的浪费**。

## 5. Terraform 方案

### 5.1 一次性准备（手动）
Homebrew 主仓库已经不提供 Terraform 了（许可证改成了 BSL），要从 HashiCorp 自己的 tap 安装；gcloud 的 cask 现在叫 `gcloud-cli`：
```bash
brew tap hashicorp/tap
```
```bash
brew install hashicorp/tap/terraform
```
```bash
brew install --cask gcloud-cli
```
本机实际情况（2026-11-15）：Command Line Tools 太旧，Homebrew 拒绝安装 formula（`CLT does not support macOS 15`），
所以 Terraform 1.17.2 是从 releases.hashicorp.com 直接下载的，核对了官方 SHA256SUMS，装在 `~/.local/bin/terraform`。
升级时重新下载并替换这个文件；修好 Command Line Tools 以后也可以改回 Homebrew 安装。gcloud 是 cask，不受影响。
登录只需要这一条（Terraform 和本项目的 gcloud 共用这一份凭据）：
```bash
gcloud auth application-default login
```
**本项目的 gcloud 命令一律用 [`infra/gcloud16s`](../infra/gcloud16s)，不要直接用 `gcloud`。** 下文命令里写的 `gcloud`，实际执行时都换成 `infra/gcloud16s`。

原因（2026-11-15 查 `~/.config/gcloud/logs/` 得出）：这台 Mac 上 Antigravity IDE 的 Google 扩展登录的是另一个 Google 账号。
`~/.config/gcloud` 里的登录信息一变，它几秒内就会带着自己的凭据文件调用 `gcloud config config-helper`
（`auth/credential_file_override` 指向 `~/Library/Application Support/google-vscode-extension/auth/`），
gcloud 随后把这份凭据存到当前账号名下，覆盖掉刚登录的那份。表现是：`gcloud auth list` 显示的账号是对的，实际发出去的 token 却是另一个账号的，所有命令返回 403。
`infra/gcloud16s` 做两件事：gcloud 的配置放在单独的文件夹 `~/.config/gcloud-agentic16s`（扩展碰不到）；凭据直接读 ADC 文件，所以不需要 `gcloud auth login`。
- 在 Console 里新建项目（例如 `agentic-16s`），绑定 billing account，然后去 Developer Program 页面把 credit 领取到这个 billing account 上。
- **不要**创建和下载 service account key。Terraform 用上面的 ADC 登录就够了。
- 用于存 state 的 GCS bucket 先手动建一个（`gs://<project>-tfstate`，打开 versioning），也可以先用 local state，之后再迁移。

### 5.2 目录结构（建议放在 `infra/`）
```
infra/
  versions.tf      # provider 和 backend
  variables.tf
  main.tf          # API、网络、防火墙、bucket、磁盘、VM
  budget.tf        # 预算告警（按扣 credit 之前的金额，只发邮件）
  killswitch.tf    # 刷卡净费用到 $1 就断开 billing（5.4b）
  killswitch/      # main.py + requirements.txt，Cloud Run function 的源码
  startup.sh       # 安装 docker、java、nextflow，挂载数据盘，空闲自动关机
  terraform.tfvars # project_id、billing_account（写进 .gitignore）
```

### 5.3 代码骨架
`versions.tf`
```hcl
terraform {
  required_version = ">= 1.9"
  required_providers {
    google  = { source = "hashicorp/google", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.0" }   # 打包 kill switch 的源码
  }
  backend "gcs" {
    bucket = "agentic-16s-tfstate"   # 改成你的
    prefix = "infra"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  # 用本机用户凭据（ADC）时必须有这两行：否则 Budget API 和 Eventarc 的配额会记到 gcloud 自己的
  # client project 上，返回 403（2026-11-15 实际踩到）
  user_project_override = true
  billing_project       = var.project_id
}
```

`variables.tf`
```hcl
variable "project_id"      { type = string }
variable "billing_account" { type = string }            # 形如 0X0X0X-0X0X0X-0X0X0X
variable "region"          { default = "us-central1" }
variable "zone"            { default = "us-central1-a" }
variable "machine_type"    { default = "e2-standard-2" }  # smoke test；全量跑用 -var machine_type=e2-standard-8
variable "data_disk_gb"    { default = 100 }
variable "budget_usd"      { default = 10 }
variable "worker_running"  { default = false }          # 默认关机：true 时 apply 会开机
variable "use_spot"        { default = false }          # credit 不付 Spot，默认关；打开就要刷卡
```

`main.tf`
```hcl
resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "storage.googleapis.com",
    "iap.googleapis.com",
    "billingbudgets.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# 结果和报告（不放 FASTQ）
resource "google_storage_bucket" "results" {
  name                        = "${var.project_id}-results"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning { enabled = true }
  lifecycle_rule {
    condition {
      age        = 30
      with_state = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }
}

# 只允许通过 IAP 隧道 SSH，不对公网开 22
resource "google_compute_firewall" "iap_ssh" {
  name          = "allow-iap-ssh"
  network       = "default"
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  target_tags   = ["agentic16s-worker"]
}

resource "google_service_account" "worker" {
  account_id   = "agentic16s-worker"
  display_name = "Nextflow worker VM"
}

resource "google_storage_bucket_iam_member" "worker_rw" {
  bucket = google_storage_bucket.results.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
}

# 数据盘独立于 VM，换机型或删 VM 都不会丢
resource "google_compute_disk" "data" {
  name = "agentic16s-data"
  type = "pd-balanced"
  size = var.data_disk_gb
  zone = var.zone
}

resource "google_compute_instance" "worker" {
  name         = "agentic16s-worker"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["agentic16s-worker"]
  desired_status = var.worker_running ? "RUNNING" : "TERMINATED"

  scheduling {
    provisioning_model          = var.use_spot ? "SPOT" : "STANDARD"
    preemptible                 = var.use_spot
    automatic_restart           = !var.use_spot
    on_host_maintenance         = var.use_spot ? "TERMINATE" : "MIGRATE"
    instance_termination_action = var.use_spot ? "STOP" : null
  }

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 30
      type  = "pd-balanced"
    }
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "data"
  }

  network_interface {
    network = "default"
    access_config {}   # 临时外网 IP，用来下载 ENA 和拉容器；不开 Cloud NAT
  }

  service_account {
    email  = google_service_account.worker.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config { enable_secure_boot = true }
  metadata = {
    enable-oslogin = "TRUE"
    startup-script = file("${path.module}/startup.sh")
  }

  allow_stopping_for_update = true
  depends_on                = [google_project_service.apis]
}

output "ssh" {
  value = "gcloud compute ssh ${google_compute_instance.worker.name} --zone ${var.zone} --tunnel-through-iap"
}
```

`budget.tf`
```hcl
data "google_project" "this" {}

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "agentic-16s monthly"
  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"   # 按扣 credit 之前的金额来提醒，才能看出 credit 快用完了
  }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }
  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.9 }
  threshold_rules { threshold_percent = 1.0 }
}
```

`startup.sh`（要点，每次开机都会运行，所以必须写成幂等）
```bash
#!/bin/bash
set -euo pipefail
# 1. 挂载数据盘（只在第一次格式化）
DEV=/dev/disk/by-id/google-data
blkid "$DEV" || mkfs.ext4 -m 0 "$DEV"
mkdir -p /mnt/data && mountpoint -q /mnt/data || mount "$DEV" /mnt/data
# 2. 安装 docker、java 21、nextflow（已经装过就跳过）
command -v docker   || (apt-get update && apt-get install -y docker.io openjdk-21-jre-headless git tmux)
command -v nextflow || (curl -s https://get.nextflow.io | bash && mv nextflow /usr/local/bin/)
# 3. 只读的状态脚本，给 Mac 上的 Claude Code 用（见 5.5）
cat >/usr/local/bin/agentic16s-status <<'EOF'
#!/bin/bash
uptime; free -h | head -2; df -h /mnt/data | tail -1
pgrep -f nextflow >/dev/null && echo "nextflow: running" || echo "nextflow: not running"
for f in /home/*/agentic-16s/studies/*/log.jsonl; do [ -f "$f" ] && echo "$f: $(tail -n 1 "$f")"; done
tail -n 15 /home/*/run.log 2>/dev/null
EOF
chmod 755 /usr/local/bin/agentic16s-status
# 4. 空闲自动关机：没有 nextflow 进程超过 60 分钟就 shutdown
cat >/etc/cron.d/idle-shutdown <<'EOF'
*/10 * * * * root pgrep -f nextflow >/dev/null && touch /run/busy; [ -n "$(find /run/busy -mmin -60 2>/dev/null)" ] || [ "$(cut -d. -f1 /proc/uptime)" -lt 3600 ] || shutdown -h now
EOF
```
（Nextflow 版本按 `versions.env` 里的 `NXF_VER=26.10.6` 来固定：运行时设置 `export NXF_VER=26.10.6`。）

### 5.4 日常使用
```bash
cd infra && terraform apply -var worker_running=true
```
```bash
gcloud compute ssh agentic16s-worker --zone us-central1-a --tunnel-through-iap
```
VM 里：
```bash
cd ~/agentic-16s && git pull && NF_PROFILE=docker DATA_DIR=/mnt/data python3 -I pipeline/bin/run_study.py 35756051
```
```bash
gcloud storage rsync -r /mnt/data/pipeline gs://<project>-results/pipeline
```
回到 Mac 后拉回结果、加载数据库，然后关机：
```bash
gcloud storage rsync -r gs://<project>-results/pipeline ~/agentic16s-data/pipeline
```
```bash
cd infra && terraform apply -var worker_running=false
```
这一批跑完、一段时间不再用的话，删掉 VM 和数据盘，只留 bucket（省掉每月 $10–13 的磁盘费）：
```bash
cd infra && terraform destroy -target=google_compute_instance.worker -target=google_compute_disk.data
```

### 5.4b 刷卡超过 $1 就全部停掉（kill switch）

owner 的要求（2026-11-15）：信用卡上一旦被扣超过 $1，就立刻停掉项目里的所有东西。

**第一道防线：免费试用本身。** $300 / 90 天的 Free Trial 写明 "No billing during trial"。
试用期间 Google 不会扣卡；credit 用完或 90 天到期以后，资源会停下来，除非你手动点 **"Upgrade" / "Activate full account"**。
**只要不点升级，卡就不会被扣钱。** 一旦升级，这道防线就没了，下面的 kill switch 就变成唯一的保护。

**第二道防线：预算触发自动断开 billing。** 预算告警本身只发邮件，所以要按 Google 文档里 "cap costs" 的做法来：
预算 → Pub/Sub → Cloud Run function → 把项目的 billing account 断开。断开以后，项目里所有收费的服务都会停掉。

- 预算看的是 **扣掉所有 credit 之后的净费用**（`INCLUDE_ALL_CREDITS`），也就是真正会落到卡上的钱，阈值设为 $1。
- **不是精确到分的硬上限。** Google 的账单数据会延迟几个小时，预算通知也不是实时的，实际停下来的时候可能已经超过 $1 一点。
  所以 VM 默认关机、闲置自动关机仍然要保留。
- 断开 billing 以后，VM 会停机，**磁盘和 bucket 一段时间后可能被删除**。结果每次都会同步回 Mac，所以这对本项目可以接受。
  恢复的方法：Console → Billing → 把项目重新链接到 billing account。
- 这套东西本身的费用：Pub/Sub 和 Cloud Run function 每个月只调用几十次，在免费额度之内。

**实际代码以 [`infra/killswitch.tf`](../infra/killswitch.tf) 为准。** 下面是最初的草稿，真正 apply 的版本比它多了两样东西：
一个专门用来构建 function 的 service account（`fn-build`），这样构建不依赖默认的 compute service account；
以及 `versions.tf` 里 provider 的 `user_project_override` 设置。

2026-11-15 第一次 apply 时踩到的坑：
- Budget 返回 403 "requires a quota project"：provider 里缺 `user_project_override = true` 和 `billing_project`。
- Function 的 Eventarc trigger 返回 "Caller does not have required permission to use project"：Eventarc API 刚开通，还没准备好。过几分钟重新 apply 就好了。
- Function 第一次收到测试消息时，在 `getBillingInfo` 返回 403：Project Billing Manager 可以断开 billing，但没有读取项目的权限。
  去掉这一步检查、直接断开以后，测试通过（2026-11-15 13:51，`billingEnabled: false`）。下面草稿里的 `getBillingInfo` 已经过时。
- 中途 `gcloud` 命令全部返回 403，看起来像项目被封了，其实是 gcloud 存的凭据被 Antigravity 的 Google 扩展换成了另一个账号的（原因和解决办法见 5.1）。
  Terraform 用的是 ADC，不受影响。检查方法：把 `infra/gcloud16s auth print-access-token` 拿到的 token 交给 `https://oauth2.googleapis.com/tokeninfo` 查，看返回的 email。

`killswitch.tf`（草稿）
```hcl
resource "google_project_service" "killswitch_apis" {
  for_each = toset([
    "pubsub.googleapis.com",
    "cloudfunctions.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "eventarc.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbilling.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

resource "google_pubsub_topic" "budget" {
  name       = "budget-alerts"
  depends_on = [google_project_service.killswitch_apis]
}

# 扣掉所有 credit 之后，净费用（= 会刷到卡上的钱）到 $1 就触发
resource "google_billing_budget" "card_cap" {
  billing_account = var.billing_account
  display_name    = "agentic-16s card cap $1"
  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = "1"
    }
  }
  threshold_rules { threshold_percent = 1.0 }
  all_updates_rule {
    pubsub_topic                     = google_pubsub_topic.budget.id
    disable_default_iam_recipients   = false   # 账号管理员照样收到邮件
  }
}

resource "google_service_account" "killswitch" {
  account_id   = "budget-killswitch"
  display_name = "Disables billing when the card cap is hit"
}

# 只给这个项目的 Project Billing Manager：只能断开这个项目的 billing，碰不到别的项目
resource "google_project_iam_member" "killswitch_billing" {
  project = var.project_id
  role    = "roles/billing.projectManager"
  member  = "serviceAccount:${google_service_account.killswitch.email}"
}

data "archive_file" "killswitch" {
  type        = "zip"
  source_dir  = "${path.module}/killswitch"
  output_path = "${path.module}/.build/killswitch.zip"
}

resource "google_storage_bucket" "fn_source" {
  name                        = "${var.project_id}-fn-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

resource "google_storage_bucket_object" "killswitch" {
  name   = "killswitch-${data.archive_file.killswitch.output_md5}.zip"
  bucket = google_storage_bucket.fn_source.name
  source = data.archive_file.killswitch.output_path
}

resource "google_cloudfunctions2_function" "killswitch" {
  name     = "budget-killswitch"
  location = var.region
  build_config {
    runtime     = "python312"
    entry_point = "stop_billing"
    source {
      storage_source {
        bucket = google_storage_bucket.fn_source.name
        object = google_storage_bucket_object.killswitch.name
      }
    }
  }
  service_config {
    max_instance_count    = 1
    available_memory      = "256M"
    service_account_email = google_service_account.killswitch.email
    environment_variables = { PROJECT_ID = var.project_id }
  }
  event_trigger {
    trigger_region        = var.region
    event_type            = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic          = google_pubsub_topic.budget.id
    retry_policy          = "RETRY_POLICY_RETRY"
    service_account_email = google_service_account.killswitch.email
  }
}

# Eventarc 用这个 SA 调用 function
resource "google_project_iam_member" "killswitch_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.killswitch.email}"
}
```

`killswitch/main.py`
```python
import base64
import json
import os

import functions_framework
from googleapiclient import discovery

PROJECT = f"projects/{os.environ['PROJECT_ID']}"


@functions_framework.cloud_event
def stop_billing(cloud_event):
    msg = json.loads(base64.b64decode(cloud_event.data["message"]["data"]))
    cost, budget = msg["costAmount"], msg["budgetAmount"]
    print(f"budget update: cost={cost} budget={budget}")
    if cost < budget:
        return
    billing = discovery.build("cloudbilling", "v1", cache_discovery=False)
    info = billing.projects().getBillingInfo(name=PROJECT).execute()
    if not info.get("billingEnabled"):
        print("billing already disabled")
        return
    billing.projects().updateBillingInfo(name=PROJECT, body={"billingAccountName": ""}).execute()
    print(f"billing disabled for {PROJECT}: cost {cost} >= cap {budget}")
```

`killswitch/requirements.txt`
```
functions-framework==3.*
google-api-python-client==2.*
```

**上线顺序：先 kill switch，后 worker。** 第一次 apply 只建 kill switch：
```bash
cd infra && terraform apply -target=google_cloudfunctions2_function.killswitch -target=google_billing_budget.card_cap -target=google_project_iam_member.killswitch_billing -target=google_project_iam_member.killswitch_invoker
```
然后测一次：往 topic 里手动发一条"已超支"的假消息，确认 billing 真的被断开，再在 Console 里重新链接上：
```bash
gcloud pubsub topics publish budget-alerts --message='{"costAmount": 2, "budgetAmount": 1}'
```

### 5.5 在 Mac 上用 Claude Code 监测 worker

做法：Claude Code（Claude Desktop）**留在 Mac 上**，用你 Mac 上的 `terraform`、`gcloud` 和 `ssh` 远程操作 VM。
你用语音下指令，Claude 在后台执行命令，再用中文回报。不推荐把 Claude Code 装到 VM 上：VM 一关机会话就断了，而且还要在 VM 上登录 Claude。

**一次性准备**

1. 在 OS Login 里注册你的 SSH 公钥。Terraform 里设了 `enable-oslogin = "TRUE"`，所以登录用户名由 OS Login 决定，注册时命令会打印出来：
   ```bash
   gcloud compute os-login ssh-keys add --key-file ~/.ssh/id_ed25519.pub
   ```
2. 在 `~/.ssh/config` 里加下面这段，之后 `ssh agentic16s-worker` 会自动走 IAP 隧道（防火墙只对 IAP 开了 22 端口）：
   ```
   Host agentic16s-worker
     User <OS Login 用户名>
     ProxyCommand /Users/chjp007/Desktop/Work/proj4newjob/agentic-16s/infra/gcloud16s compute start-iap-tunnel agentic16s-worker 22 --listen-on-stdin --zone us-central1-a
     ServerAliveInterval 30
   ```
3. `tmux` 已经加进 `startup.sh` 的 `apt-get install` 那一行了，开机就会装好。

**在 tmux 里跑，SSH 断开也不影响**

```bash
ssh agentic16s-worker 'tmux new-session -d -s run "cd ~/agentic-16s && NF_PROFILE=docker DATA_DIR=/mnt/data python3 -I pipeline/bin/run_study.py 35756051 --subset 6 2>&1 | tee ~/run.log"'
```
需要的时候，你自己也能连上去看实时输出：`ssh -t agentic16s-worker tmux attach -t run`。

**Claude 用来查状态的命令**

| 想知道的 | 命令 |
|---|---|
| VM 开着吗 | `gcloud compute instances describe agentic16s-worker --zone us-central1-a --format='value(status,machineType.basename())'` |
| 跑到哪个 stage | `ssh agentic16s-worker 'tail -n 5 ~/agentic-16s/studies/35756051/log.jsonl'` |
| 屏幕输出最后几行 | `ssh agentic16s-worker 'tail -n 30 ~/run.log'` |
| Nextflow 每个 task 的状态 | `ssh agentic16s-worker 'tail -n 20 $(ls -t /mnt/data/pipeline/35756051/*/ampliseq/pipeline_info/execution_trace*.txt \| head -1)'` |
| 机器负载 | `ssh agentic16s-worker 'uptime; free -h; df -h /mnt/data; docker stats --no-stream'` |
| 出错原因 | 先看 `.nextflow.log`，再看失败 task 目录下的 `.command.err` |
| 还没开机就连不上时 | `gcloud compute instances get-serial-port-output agentic16s-worker --zone us-central1-a \| tail -50`（看 startup script 有没有出错） |

**后台盯着，有事再叫你**

跑全量 study 时，可以对 Claude 说"盯着这个 run"。Claude 会在后台开一个监测任务，大约每 5 分钟执行一次 `ssh agentic16s-worker agentic16s-status`（这条已经在允许列表里，不用每次确认）。
下面这些情况出现时，Claude 会收到通知，再告诉你（也可以推送到手机）：
- 某个 stage 完成
- 出现 `failed` 或 `needs_review`
- VM 变成 `TERMINATED`

不要让监测频率太高：每次查看都是一次 SSH 连接，查得太勤没有意义。

**权限：只读的命令免确认，花钱或删东西的命令每次都要确认**

在项目的 `.claude/settings.json` 里加一个允许列表：
```json
{
  "permissions": {
    "allow": [
      "Bash(infra/gcloud16s compute instances describe:*)",
      "Bash(infra/gcloud16s compute instances list:*)",
      "Bash(infra/gcloud16s compute instances get-serial-port-output:*)",
      "Bash(terraform plan:*)",
      "Bash(terraform output:*)",
      "Bash(ssh agentic16s-worker agentic16s-status)"
    ]
  }
}
```
- `agentic16s-status` 是放在 VM 上的一个固定脚本（例如 `/usr/local/bin/agentic16s-status`，由 `startup.sh` 写入）。
  它只做只读的事：打印 VM 负载、磁盘占用、`run.log` 最后几行，以及每个 study `log.jsonl` 的最后一行。
  允许列表里**只放这一条精确的命令**，不加 `:*`，所以它不能带别的参数。
- **不要**写成 `ssh agentic16s-worker 'tail:*` 这种前缀规则。远程命令是放在引号里的一整个字符串，`'tail x; rm -rf ...'` 也会匹配上同一个前缀。
  上面表格里其他的 `ssh` 命令，每次照样要你确认。
- `terraform apply`、`terraform destroy` 也**不要**放进允许列表，每次都由你确认。

## 6. 分阶段做

1. **阶段 1：已完成（2026-11-15）。** 实际代码在 `infra/worker.tf`、`infra/startup.sh`（以它们为准，上面 5.3 的骨架是早期草稿）。
   - 机型 `e2-standard-8`，普通 VM，自建 VPC `agentic16s-net`，只开 IAP SSH，50 GB 启动盘，没有单独的数据盘。
   - 代码用 tar + `infra/gcloud16s compute scp` 传上去（repo 不是 git）；`NF_PROFILE=docker` 现在可以用环境变量覆盖（`pipeline/bin/common.py`）。
   - 第一次在 fetch 之后崩溃：`resource_monitor.Sampler` 的 `self._stop` 和 Python 3.12 的 `Thread` 内部方法重名（Mac 是 3.9.6，不触发）。已修复并加了测试。
   - 第二次全部成功：fetch 2 分钟，ampliseq 20 分钟（含拉镜像、下载 SILVA），VM 从开机到关机约 30 分钟，约 $0.14。
   - **和 Mac 结果对比：6 个 run 每一步的 read 数、389 个 ASV（序列一致）、860 行 abundance 全部相同**；只有 7 个稀有 ASV（≤ 69 条 read）的分类 bootstrap 置信度不同，其中 2 个跨过 0.5 阈值导致 genus/species 标签不同。
     详见 `docs/demo-stories.md` 第 3 个故事；VM 的 QC 文件是 `studies/35756051/pipeline_qc_subset6_gce.json`，结果表在 `~/agentic16s-data/gce/35756051/`。
   - SSH 细节：第一次 `compute ssh` 要求 `infra/gcloud16s` 配置里有 `core/account`（已设好），新登记的 OS Login 公钥要等几十秒才生效。
2. **阶段 2：已完成（2026-11-15 20:25 UTC → 11-16 12:56 UTC，VM 约 16.5 小时，约 $4.5 试用 credit）。**
   prepare 在 Mac 上跑（要读数据库里的 run 状态），VM 从 `--from preflight --no-db` 开始；结果拉回 Mac，用 `run_study.py <PMID> --from load` 入库。

   | Study | run | 去噪 | ASV | QC | 说明 |
   |---|---|---|---|---|---|
   | 29335008 | 38 | pooled | 1868 | pass | 46 分钟 |
   | 35756051 | 89 | pseudo（16 分钟） | 1877 | review | 第一次因 ENA 坏文件失败；加了 ENA→NCBI 逐 run 补下载后通过（demo story 4）。42 个 run 质量过滤后剩不到 20% |
   | 37661264 | 97 | pooled（约 3.5 小时） | 10707 | pass | 物种分类单线程、每块 15–16 GB，只能串行，约 5 小时 |
   | 28174747 | 90 | pseudo（1 小时 16 分） | 3651 | review | 1×101 单端，ASV 全部 101 nt；QC 的长度检查按完整扩增子判断，属于规则问题 |

   这一阶段学到的（细节在项目记忆和 `run_study.py` 注释里）：
   - 真正的瓶颈是 DADA2 的 **pooled 去噪**和**物种分类（addSpecies）**，两者都只用 1–3 个核。已改成 pseudo；addSpecies 要么跳过，要么换 64 GB 机器（待决定）。
   - 不要给 nf-core 的步骤写固定内存：会关掉"失败后加内存重试"。现在 addSpecies 的内存随重试加倍。
   - 新代码传上 VM 后，对已经启动的 `run_study.py` 进程不起作用。
   - Python 3.12 暴露了 `resource_monitor` 的线程 bug（Mac 是 3.9）。

   原计划（保留作对照）：五个 study 换到 `e2-standard-8` 上依次跑（`terraform apply -var machine_type=e2-standard-8 -var worker_running=true`），结果同步回 Mac 加载进数据库。
3. **阶段 3（Phase 7 扩容）**：（2026-11-16 改了做法：每个 study 一个 Batch job，不用 per-task executor，见第 8 节。）原计划是改用 Nextflow 的 `google-batch` executor，`workDir = gs://...`，每个 process 一台 VM（用 credit 就选普通 VM，刷卡才选 Spot），Terraform 再加上 Batch 的 IAM。
   这一步才真正发挥云的弹性，但调试成本也更高，等 study 数量上来以后再做。

## 7. 待你确认
- ~~Pro 每月给多少 credit、会不会过期~~：已核实，每月 $10，12 个月有效，见 1.1。
- 领取以后，到 Console → Billing → Credits 确认这笔 credit 的适用范围包括 Compute Engine（这一点只能在你的账号里看到）。
- 是否另外开 $300 的免费试用（需要绑卡；它的排除规则我没有核实）。
- 数据库是否接受"留在 Mac、VM 只产出 tables"的方案。
- `infra/` 放进公开 repo（`terraform.tfvars` 和 state 不放进去），可以展示 IaC 能力，推荐这么做。

## 8. Google Batch：每个 study 一个 job（2026-11-16）

### 8.1 为什么做

- **作品集**：Google Batch 是托管的批处理服务。"提交 job、跑完自动删 VM"比"自己开一台 VM、跑完关机"更像真实的云上生信流程。
- **不用管 VM**：没有常驻 VM，不需要开关机，也不需要空闲关机的 cron。每个 job 有最长运行时间，日志进 Cloud Logging。Batch 本身不另收费，只付 VM 的钱。
- **不是为了更快**：机型还是 `e2-standard-8`，一个 study 的耗时应该和 GCE VM 差不多（8.6 节对比实测）。
  以后 study 多了，可以在 12 vCPU 配额内同时跑 2–3 个 job，这才是省时间的地方。

### 8.2 为什么是"每个 study 一个 job"，不是 Nextflow 的 per-task executor

阶段 3 原来计划用 Nextflow 的 `google-batch` executor（每个 task 一台 VM、`workDir = gs://...`）。看了阶段 1/2 的记录后改了：

| | 每个 study 一个 job（现在的做法） | 每个 task 一台 VM |
|---|---|---|
| VM 开机开销 | 每个 study 一次（1–2 分钟） | ampliseq 一次有 180–380 个 task，大多只跑几秒（FASTQC 4 s、FILTNTRIM 8 s），开机时间比计算时间还长 |
| 配额（12 vCPU） | 一个 job 用 8 | 并行度同样被 12 卡住，弹性用不上 |
| `run_study.py` 的检查 | 照常：integrity 检查、ENA→NCBI 逐 run 补下载（E10）、resource_monitor 都要本地文件 | work dir 在 GCS，这些都得改写 |
| 已有经验 | 完全复用验证过的 Docker 路径，E9–E16 的修复都在 | 新路径，要重新踩坑 |
| 代价 | job 中途失败时，Nextflow work dir 随 VM 一起删掉，整个 study 要从头跑（20–80 分钟） | 可以 `-resume` |

最后一行的代价可以接受：job 本来就是按 study 切的，跑一次的成本是几毛钱。

### 8.3 组成

- `infra/batch.tf`：开启 Batch 和 Logging API；建 bucket `<project>-batch`（Standard，30 天后自动删除；Mac 上保留长期副本）；建 service account `agentic16s-batch`。
  这个账号只有三类权限：向 Batch 汇报、写日志，以及对这个 bucket 的读和新建对象，覆盖和删除都做不了。
  VM 用 worker 的 VPC 和临时外网 IP，不需要防火墙规则，也不需要 Cloud NAT（约 $30/月）。
- `infra/batch_runner.sh`：job 里跑的脚本，以 root 身份在 Batch 的 Debian 镜像上运行。
  装的工具和 `startup.sh` 一样（Docker、Java、Nextflow、sra-tools）；执行 `run_study.py <PMID> --from preflight --no-db`；最后把结果表、报告、日志和整机内存采样上传到 bucket，不上传 FASTQ。
  `MODE=probe` 只检查环境，不跑 study。
- `infra/batch_submit.py`：在 Mac 上一条命令完成全部流程：检查 → 准备输入 → 上传代码快照 → 提交 → 等待 → 取回结果 → 合并 `studies/<PMID>/` → 入库或对比。
  每个 job 都记进 `studies/<PMID>/batch_runs.jsonl`，包括状态、分钟数和估算费用。
- `pipeline/bin/compare_tables.py`：两次运行逐表对比，包括参数、每个 run 的 read 数、ASV 序列、abundance 的每个格子，以及每一级的分类标签。

### 8.4 以前的教训落在哪里

| 教训 | 设计里的对应 |
|---|---|
| E13：改了代码，已经在跑的进程不受影响 | 每个 job 带一份固定的代码快照（`code/<sha>.tgz`），之后的修改不会混进正在跑的 job |
| E11：pooled 去噪跑了几个小时，事前没有估计 | 提交前打印这个 study 的 run 数、read 数，并列出已实测 study 的耗时 |
| E9：Python 3.12 的 bug 在 Mac 的 3.9 上看不出来 | 探测 job 在 Batch 镜像自己的 Python 上跑全部单元测试 |
| E16：Docker 下整条流程的内存峰值被少算 | runner 每 10 秒记一次整机内存（`vm_memory.tsv`） |
| E12：给 nf-core 步骤写死内存会关掉重试 | `resources.config` 不变，还是由 `run_study.py` 按机型生成 |
| 空闲关机靠 cron | 改用 `maxRunDuration` 加 runner 里的 `timeout`，到时间一定停 |
| Spot 不能用 credit 付 | `provisioningModel: STANDARD`；机型只允许 e2 和 n2d |
| 配额 12 vCPU，旧 VM 开着会占 8 | 提交前统计旧 VM 和正在跑的 job 占了多少 vCPU，超过配额就不提交 |
| 两个 Google 账号，token 会被换掉 | 提交前用 tokeninfo 核对 token 的 email |
| E17：项目第一次提交的 job 8 秒就失败 | 新项目先跑一个探测 job，第一次失败就隔几分钟重交 |
| E18：Batch 镜像是 Debian 12，和 VM 的 Ubuntu 不同 | 装包前用 `apt-cache policy` 判断有没有可装的版本，Java 装不了 21 就用 17 |
| E19：Batch 不导出 PATH，Python 找不到 `/usr/local/bin/nextflow` | runner 开头导出完整 PATH；探测和开跑前都从 Python 的角度检查工具 |
| E20：每个 job 是新 VM，每次都要拉镜像，一次断线就让整个 study 失败 | fetch 期间在后台预拉 `pipeline/containers.txt` 里的 13 个镜像，失败重试 5 次；拉完才开始 ampliseq |
| E21：关掉 Mac 上未签名的 `gcloud-crc32c` 后，大文件下载被跳过 | `infra/gcloud16s` 改成整个对象传输并用 MD5 校验；`collect` 会打印 gcloud 的报错 |

### 8.5 探测结果（Batch 的 Debian 镜像，2026-11-16）

`python3 -I infra/batch_submit.py --probe`，e2-standard-2，第三次成功（`a16s-probe-20261117-002105`）：

| 项目 | Batch `batch-debian` | GCE worker（Ubuntu 24.04） |
|---|---|---|
| 系统 | Debian 12 (bookworm)，内核 6.1 | Ubuntu 24.04 |
| 自带 | gcloud 592.0.0、Docker 29.9.3、Python 3.11.2、curl | 无 Docker，要装 |
| Java | 只有 17（17.0.21）；Nextflow 26.10.6 在 17 上正常 | 21 |
| sra-tools | 3.0.3（Debian 包） | 3.0.3 |
| 网络（临时外网 IP） | ENA、NCBI SDL、GitHub 都是 200，能从 quay.io 拉镜像 | 同 |
| 单元测试 | 49 个在 Python 3.11 上全部通过 | — |

时间：提交 → RUNNING 约 1 分钟（QUEUED → SCHEDULED → RUNNING）；装工具 58 秒；整个探测在 VM 上跑了 106 秒，估算费用 $0.004。

前两次失败（都没花钱）：
1. E17：项目第一次提交的 job 8 秒就失败，因为 Batch 的 service agent 要到第一次提交时才创建。过几分钟重交即可。
2. E18：Debian 12 装不了 openjdk-21，但 `apt-cache show` 照样返回成功。现在改用 `apt-cache policy` 判断，装不了就用 17。

### 8.6 实测

**冒烟测试：35756051 的 6 个 run（2026-11-16，`a16s-35756051-20261117-003156`，e2-standard-8）**

- 第二次成功。第一次因为 Batch 不导出 PATH（E19），在 fetch 停住。
- 时间：提交 → RUNNING 约 1 分钟；VM 上 19.8 分钟（装工具约 1 分钟、preflight 1 分钟、fetch、ampliseq 约 15 分钟，含拉镜像和下载 SILVA）；估算 $0.10。
  GCE VM 上同样 6 个 run：fetch 2 分钟 + ampliseq 20 分钟，VM 开机到关机约 30 分钟。
- 和参照（Mac/GCE 那两次，pooled 去噪）对比：参数只差 `sample_inference`（pooled → pseudo）和 `skip_dada_addspecies`，都是 11-15/16 定下的固定参数。
  6 个 run 的 `reads_input`、`reads_after_trim`、`reads_filtered` 全部相同；`reads_denoised` 及之后的步骤不同（pseudo 的预期结果）。
  ASV 309 个（参照 389 个），non-chimeric read 总数 217,766（参照 218,651）。257 个共同 ASV 里，genus 和 species 标签各只有 1 个不同。
  QC 判定三次相同：`review`，理由也相同（SRR15400843 质量过滤后只剩 3%）。
- 内存：整机 `free` 采样显示，DADA2_TAXONOMY 开始后的约 70 秒冲到 26.9 / 32 GB，trace 只记了 9.6 GB（E16 的补充）。所以有 SILVA 分类的 study 至少要 32 GB。
- 结论：流程本身没问题。结果要和参照完全一致，得用同样的参数，见下面的全量对比。

**加了预拉之后再跑一次冒烟（`a16s-35756051-20261117-020003`）**

- VM 上 17.4 分钟，估算 $0.09。13 个镜像 167 秒拉完，比 fetch 结束得早，所以没有增加总时间。
- 这次又遇到一次和 E20 一样的 `connection reset by peer`（`bash_coreutils` 镜像），预拉重试一次就成功了。没有预拉的话，这个 study 会失败。
- **两次 Batch 冒烟互相对比**（参数和代码路径相同）：6 个 run 的所有 read 数、309 个 ASV、518 个 abundance 格子、所有 genus 标签全部相同；只有 1 个 species 标签不同。genus 层面可复现。

**第一次全量（`a16s-35756051-20261117-005523`）失败**：fetch 用了 30 分钟（ENA 下载 2.2 GB，约 1.2 MB/s；SRR15400852 照旧从 NCBI 补下载），ampliseq 开始 5.5 分钟后拉镜像断线，docker exit 125（E20）。花了 38.5 分钟，约 $0.18。

**全量重跑（`a16s-35756051-20261117-022001`，89 个 run，e2-standard-8）成功**

- 时间（实测）：提交 → RUNNING 约 2 分钟；VM 上共 102.2 分钟，估算 $0.47（按标准价，STANDARD 预配）。
  其中：装工具 + preflight 2 分钟，fetch 28 分钟（2.2 GB），ampliseq 71.6 分钟（02:51:37 → 04:03:10）。
  预拉：13 个镜像 158 秒拉完，0 个失败，在 fetch 期间完成，没有增加总时间。
- **和 GCE VM 那次（11-16，同样 pseudo 去噪）对比：参数只差 `skip_dada_addspecies`。**
  89 个 run 的所有 read 数相同；1877 个 ASV 相同；6868 行 abundance、2,013,868 条 read 逐格相同；
  domain 到 species 所有分类标签 0 处不同。QC 判定相同（`review`，质量过滤中位保留率 38% < 60%）。
  结果在 `studies/35756051/batch_compare.json`、`pipeline_qc_batch.json`，job 记录在 `batch_runs.jsonl`。
- ampliseq 耗时：GCE 73 分钟（含 addSpecies 13.4 分钟 CPU 时间、约 9 分钟墙钟），Batch 71.6 分钟（不含 addSpecies）。
  所以同样的步骤 Batch 慢了约 10–15%：按 trace 的 realtime 合计，DADA2_FILTNTRIM 32.9 → 35.4 分钟，
  DADA2_DENOISING 16.0 → 19.6，DADA2_QUALITY1 14.7 → 18.1，DADA2_TAXONOMY 9.7 → 12.2；FASTQC 反而快一点（14.1 → 13.2）。
  每边只跑了一次，原因没有确认。可能是 e2 机型每次分到的 CPU 平台不同，也可能是 Debian 12 和 Ubuntu 24.04 的差别。要下结论，需要固定 CPU 平台（换 n2/c3）再跑一次。
- 内存（实测，整机 `free` 每 10 秒采样）：峰值 26.5 / 32 GB，04:01:53，在 DADA2_TAXONOMY 期间。和冒烟测试一致，32 GB 是下限。
- 结论：Batch 一个 job 跑完一个完整 study，结果和 GCE VM 完全相同，不需要人管 VM 的开关机。
