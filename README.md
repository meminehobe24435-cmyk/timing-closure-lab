# timing-closure-lab

**一个时序收敛实验台**：用真实的开源数字设计流程（`yosys` 综合 → `nextpnr-ecp5` 布局布线 → 时序报告）跑一条**带 SDC 时序约束的 block 级流程**，用 TCL 写约束、用 Python 分析时序报告，并**真的做一次时序优化、量出 Fmax 改善**。

一条命令跑完：

```bash
py -3.12 -m tclab run --out reports
```

---

## ⚠️ 先说清楚这是什么

**这不是 ASIC 后端，也没有任何签核级工具。** 请先读完这一段再看数字。

| 项目 | 真实情况 |
|---|---|
| **目标工艺** | **Lattice ECP5 FPGA（LFE5U-45F / CABGA381 / speed-6）**，不是任何 ASIC 工艺节点 |
| **工具链** | 全部是开源工具：`yosys 0.66+181`、`nextpnr-ecp5 0.10-82`、`iverilog 14.0`，来自 OSS CAD Suite |
| **工艺库** | **没有**。没有 `.lib` / `.lef` / `.db`，没有标准单元库，没有 SPEF/寄生参数反标 |
| **STA** | 是 FPGA 工具自带的**建立时间（max-delay）分析**，不是 PrimeTime/Tempus 级签核 STA。**没有 hold 要求可查**（见下） |
| **PV / 物理验证** | **没有** DRC / LVS / ERC / antenna。本项目里的"PV 相邻能力"仅指**逻辑等价检查（LEC）** |
| **功耗** | **相对值，不是毫瓦**。没有工艺库就没有电容和电压，模型见下文，明确声明是相对估算 |
| **面积/功耗数值** | 是 FPGA 资源计数（LUT/FF/BRAM/DSP）和相对翻转率，**不是真实芯片的 mm² 或 mW** |
| **多电压域** | **没有** UPF / CPF，没有电源域划分、隔离单元、电平转换器 |
| **综合后的数字** | 每一个都来自真实运行并在 `reports/` 里保留了原始输出，**没有一个数字是编的或估的** |

一句话：这是一个**用 FPGA 开源流程做的时序收敛实验**，它复刻的是数字后端工程师的**工作方法与分析思路**，不是后端的签核结论。

---

## 为什么做这个

校招 JD 里的"数字后端 / 时序收敛"通常要求：理解 SDC 约束、会看 STA 报告、能定位关键路径、会做时序优化、了解等价性检查与低功耗、有 TCL/Python 脚本能力。

问题是：这些能力在真实项目里由昂贵的签核工具承载，个人拿不到。但这个能力栈的**核心是方法**——约束怎么写、报告怎么读、瓶颈怎么找、优化怎么量、等价性怎么证——这些完全可以用开源流程真做一遍。

所以这个项目的目标是把**每一个关键词都落成一个能跑、能测、有真实数字的动作**，而不是写一份"我了解 STA"的简历。它同时也诚实标注哪些环节替代不了签核工具。

---

## 快速开始

### 1. 准备 OSS CAD Suite

Windows（本项目实测路径）：

```bat
D:\oss-cad-suite\environment.bat
```

Linux / CI：解压 OSS CAD Suite 后 `source /path/to/oss-cad-suite/environment.sh`。

### 2. 用包装脚本调用 EDA（推荐，本地与 CI 一致）

所有 EDA 调用都经过 `tools/oss.bat`（Windows）/ `tools/oss.sh`（POSIX），这样上层代码不需要知道工具装在哪：

```bat
@echo off
call "D:\oss-cad-suite\environment.bat" >nul 2>&1
%*
```

验证：

```bash
py -3.12 -m tclab env
```

```
tclab 1.0.0
wrapper   : tools/oss.bat
versions:
  yosys            Yosys 0.66+181 (git sha1 afe6b18f2, Release, ...)
  nextpnr-ecp5     "nextpnr-ecp5" -- Next Generation Place and Route (Version nextpnr-0.10-82-g2b560ad0)
  iverilog         Icarus Verilog version 14.0 (devel) (s20260301-227-g5a99d0e44-dirty)
```

> 若工具装在别处，设 `TCLAB_WRAPPER=<你的包装脚本>` 即可。

### 3. 跑完整流程

```bash
py -3.12 -m tclab run --out reports
```

它会：综合 5 个变体 → 布局布线 → 解析报告 → 跑 RTL 自检仿真 → VCD 翻转率与功耗 → 逻辑等价检查 → 写出 `reports/timing_summary.json` 和 `reports/sta_report.md`。

### 4. 其它子命令

```bash
py -3.12 -m tclab sta reports/baseline_report.json   # 分析已有的 nextpnr 报告
py -3.12 -m tclab sdc constr/fir_block.sdc           # 解析并打印 SDC 约束
py -3.12 -m tclab power sim_out/activity_full.vcd sim_out/activity_gated.vcd
py -3.12 -m tclab lec                                # 逻辑等价检查
py -3.12 -m tclab sim                                # RTL 自检仿真
```

### 5. 跑测试

```bash
py -3.12 -m pytest tests -q        # 266 passed
```

---

## 关键结果

> 全部来自本机一次真实运行（LFE5U-45F / CABGA381 / speed-6，目标 **100 MHz**）。原始输出保留在 `reports/`：每个变体都有 `*_synth.log`（yosys）、`*.log`（nextpnr）、`*_report.json`（时序报告）。汇总见 `reports/timing_summary.json` 与 `reports/sta_report.md`。

### 综合资源与 Fmax

| 变体 | 说明 | LUT4 | FF | DSP | BRAM | **Fmax (MHz)** | 关键路径 (ns) | setup slack (ns) | 达标 |
|---|---|---|---|---|---|---|---|---|---|
| `baseline` | 串行累加链（15 级加法器串联） | 1132 | 331 | 16 | 0 | **50.971** | 19.619 | **−9.619** | ✗ |
| `tree` | 平衡加法树，**零额外延迟**（仅重结合） | 1136 | 331 | 16 | 0 | 49.801 | 20.080 | −10.080 | ✗ |
| `tree_pipe1` | 加法树 + 1 级流水（+1 拍） | 826 | 627 | 16 | 0 | 88.324 | 11.322 | −1.322 | ✗ |
| **`pipe`** | **加法树 + 2 级流水（+2 拍）** | **641** | **775** | 16 | 0 | **94.607** | **10.570** | **−0.570** | ✗ |
| `pipe_notmdriv` | 同上，但**关闭时序驱动布局**（对照组） | 641 | 775 | 16 | 0 | 76.464 | 13.078 | −3.078 | ✗ |

**Fmax：优化前 50.971 MHz → 优化后 94.607 MHz，提升 +85.6%（+43.64 MHz）。**
延迟代价：+2 拍（1 → 3 拍）。全部 5 个变体仍未收敛到 100 MHz，最接近的 `pipe` 还差 **0.570 ns** 的 setup slack——这是一个**尚未收敛完成**的设计，没有假装它达标。

### 优化过程：每次改了什么、Fmax 从多少到多少

| 步骤 | 改了什么 | 延迟 | Fmax 前 → 后 | Δ |
|---|---|---|---|---|
| `tree` | 运算重结合：串行链 → 平衡树 | +0 拍 | 50.971 → 49.801 MHz | **−2.3%** |
| `tree_pipe1` | 树中插 1 级寄存器 | +1 拍 | 50.971 → 88.324 MHz | +73.3% |
| **`pipe`** | 树中插 2 级寄存器 | +2 拍 | 50.971 → 94.607 MHz | **+85.6%** |
| `pipe_notmdriv` | 对照组：关掉时序驱动布局 | +2 拍 | 94.607 → 76.464 MHz | **−19.2%** |

三个结论，都是真实数字逼出来的：

1. **单纯重结合没用，甚至略微变差**（49.801 vs 50.971）。原因是 ECP5 上串行加法链被映射到 **CCU2C 进位链**（`--45c` 前的 carry LUT），进位链本身很高效，而关键路径里 **routing 占了 11.269 ns / 19.619 ns ≈ 57%**——瓶颈在网络延迟而不是逻辑深度。
2. **流水的收益极大**，因为它同时缩短了逻辑级数（15 → 4 级加法）*和* 布线跨度（19.619 → 10.570 ns）。
3. **时序驱动布局单独值 +23.7%**（94.607 vs 76.464），说明在 FPGA 上"布局"和"逻辑"是两个可分的杠杆。

### 关键路径

| | baseline | pipe（最优） |
|---|---|---|
| 起点 | `u_core.sr[6]`（样本移位寄存器某位 FF） | `u_core.sr[8]` |
| 终点 | `u_core.dout`（输出寄存器） | `u_core.sum_pre`（第二级流水寄存器输入端） |
| 总延迟 | **19.619 ns**（77 步） | **10.570 ns**（63 步） |
| 逻辑 | 7.825 ns（39.9%） | 5.740 ns（54.3%） |
| **布线** | **11.269 ns（57.4%）** | 4.305 ns（40.7%） |
| clk-to-q | 0.525 ns | 0.525 ns |
| 隐含 Fmax | 50.971 MHz | 94.607 MHz |

baseline 的关键路径被 `u_core.sum_pre_*`（串行累加链的组合逻辑）和 `u_core.prod[6][2]`（乘法器输出布线）主导——正是"串行链 + 长布线"。`pipe` 把链打断后，路径缩短了近一半。

### 翻转率与功耗（相对估算）

三个场景，同一个 `fir_block_top`，同一套 RTL，只改输入活动率（3000 拍，30.295 µs 仿真窗口）：

| 场景 | 说明 | 翻转网络数 | 总位翻转 | P_rel（无量纲） | 相对 full |
|---|---|---|---|---|---|
| `full` | 每拍换一个随机样本 | 19 | 2,246,673 | 7.536e+10 | 1.0000 |
| `gated` | 每 4 拍换一个（占空比 1/4） | 19 | 807,247 | 2.784e+10 | **0.3695（−63.05%）** |
| `idle` | 恒定输入 | 17 | 21,955 | 1.923e+09 | 0.0255（−97.45%） |

分类拆分印证了物理直觉：**时钟网络功耗完全不变（比值恒为 1.0000）**，减少的全部是数据网络（`gated` 下数据类比值为 0.3558）。这正是"降低活动率省的是数据功耗，不省时钟功耗"的量化体现。

**模型（明确声明是相对值）**：

```
P_rel = Σ_net ( class_weight(net) × bit_toggle_rate(net) )
```

- `bit_toggle_rate` 是**从 VCD 真实测出来的**（按位翻转次数 / 仿真时长），不是估的；
- `class_weight` 是**无量纲权重**（clock 4.0、reset 1.5、data 1.0），用来替代 `C·V²`，**不是任何库提取的电容**；
- 没有工艺库、没有寄生参数、没有时钟树模型、**没有漏电**、没有 I/O 功耗、没有毛刺功耗；
- 结果**只能横向比**（同一设计不同活动率之间），**不能读成 mW**。这不是签核级功耗分析。

### 逻辑等价检查（LEC）

| 检查 | 主张 | 口径 | 结果 |
|---|---|---|---|
| `retiming_pipeline_vs_reference` | 流水化实现与未流水化加法树功能等价 | **`pipe.dout(t) == tree.dout(t−2)`**，即输出序列延迟 2 拍后一致 | ✅ **PROVEN** |
| `reassociation_chain_vs_tree_full_width` | 平衡树与串行链算同一个和（37 bit × 16 tap） | 组合等价，无延迟偏移 | ⚠️ **未收敛（60 s 超时）** |

**流水线的口径讲清楚**：流水线**不**在同一时刻产生同一个值——那是错的。被证明的命题是 `fir_top_pipe.dout(t) == fir_top_tree.dout(t − 2)`，`LATENCY_SHIFT = PIPE_STAGES = 2`。`rtl/lec_pipe_wrapper.v` 把参考输出延迟恰好 2 拍，从复位起**每一拍**都断言相等，所以不存在"等流水线填满"的豁免窗口。

**关于第二个检查，必须说实话**：位级 CNF-SAT 证明**两个结构不同的加法器网络**等价，是 resolution 的已知困难问题，在这套工具链里**没有收敛**。实测：

| 配置 | 结果 |
|---|---|
| W=37, 16 tap（全宽） | 14639 变量 / 39944 子句，**跑了 2018 秒（33.6 分钟）无结果** |
| W=12, 16 tap | > 400 s 无结果 |
| W=6, 16 tap | > 45 s 无结果 |

所以**这一条我没有证明，也没有把它包装成"已证明"**。它由另外两条证据覆盖：

1. **自检仿真**：`sim/tb_fir.v` 对 `fir_top_chain` / `fir_top_tree` / `fir_top_pipe` 三个变体各跑 **720 次逐拍比对**（共 2160 次），全部与行为级黄金模型在其声明延迟下**零失配**。链与树因此是**传递等价**的。
2. **组合式 LEC 的方法论说明**：乘法器阵列在两个变体里是**逐字相同的 RTL**（同一表达式、同一输入、同一系数），所以 `prod_flat` 是合法的切割点；真正有证明义务的是**加法网络的重结合**，而它恰好是 SAT 最不擅长的形状。

`flow/lec_sum_equiv_scaling_probe.ys` 保留了缩小配置的探测脚本，便于复现上面的缩放数据。

### 测试

**266 passed**（`py -3.12 -m pytest tests -q`，约 54 秒）。其中 256 项不依赖 EDA 工具，10 项是真实跑综合/布局布线/仿真/形式验证的集成测试（无工具时自动跳过）。

---

## 「数字后端关键词 → 本项目怎么做的 → 真实数字」

| 关键词 | 本项目怎么做的 | 真实数字 |
|---|---|---|
| **Placement & Routing** | `nextpnr-ecp5 --45k --package CABGA381 --speed 6`，用 `--no-tmdriv` 做时序驱动布局对照组 | 时序驱动布局 ON **94.607 MHz** vs OFF **76.464 MHz**，**+18.143 MHz（+23.7%）** |
| **Block level STA** | 解析 `nextpnr --report --detailed-timing-report` 的 JSON：Fmax / 每条路径逐级时延 / 按终点与驱动分组的瓶颈 | 关键路径 **19.619 ns → 10.570 ns**；布线占 **11.269 ns（57.4%）**；slack **−9.619 → −0.570 ns** |
| **时序约束 SDC** | 手写 `constr/fir_block.sdc`（8 类命令全含）+ 自研 TCL/SDC 子集解析器 `tclab/sdc.py`（花括号、命令替换、变量、续行、带行号报错） | 8/8 类命令解析成功；`create_clock` 真的被 nextpnr 读取（日志 `Info: constraining clock net 'clk' to 100.00 MHz`）；生成的 `/2` 时钟推导周期 **20.0 ns（50 MHz）** |
| **关键路径优化** | ① 运算重结合（串行链→平衡树）② 树中插 1/2 级流水 ③ 时序驱动布局开关对照 | **50.971 → 88.324（+1 级）→ 94.607 MHz（+2 级）**，即 **+85.6%**，代价 +2 拍延迟；资源 LUT4 **1132 → 641**、FF **331 → 775** |
| **逻辑等价（PV 相邻）** | yosys `equiv_make/equiv_simple/equiv_induct` + `miter` + `sat -prove-asserts`，证明流水化后**延迟若干拍输出一致** | 流水等价 **PROVEN**（`dout(t) == tree.dout(t−2)`，从复位起逐拍断言）；重结合全宽 CEC **33.6 分钟未收敛**，如实标注未证明 |
| **低功耗（翻转率与功耗估算）** | `iverilog` dump VCD → 自研 VCD 解析器逐位统计翻转率 → 相对动态功耗模型；对比全速/占空比/静止三场景 | 占空比 1/4：**P_rel × 0.3695（−63.05%）**；静止：**× 0.0255**；其中**时钟功耗比值恒为 1.0000**，降的全是数据网络（0.3558） |
| **TCL 脚本** | 实测确认 `yosys 0.66+181` **内建 TCL 8.6.12**（`-c`/`--tcl-scriptfile`），因此流程脚本用 TCL 写：`flow/*.ys` 与 LEC 脚本；SDC 本身就是 TCL 方言 | `-c file.tcl` 与 `yosys -import` 实测可用；`-p`/`-s` 脚本化流程脚本 6 个 |
| **Python 分析** | 7 个模块：`sdc` / `report_parse` / `sta` / `power` / `lec` / `flow` / `runner` / `cli`，无第三方运行时依赖 | 解析器覆盖真实报告 367 条网络时序、6 条关键路径；**266 项 pytest**；一条命令跑完整流程 |

---

## 已实现的范围

- **RTL**：16 抽头定点 FIR，`16×16` 有符号乘 + 37 bit 累加器；参数化求和网络（串行链 / 平衡树 / 流水级数）；双时钟域（主时钟 + 分频 `clk_div2` 生成时钟），使 SDC 里的 `create_generated_clock` / `set_clock_groups` 指向真实对象。
- **SDC**：`create_clock`、`create_generated_clock`、`set_input_delay`、`set_output_delay`、`set_max_delay`、`set_false_path`、`set_multicycle_path`、`set_clock_groups` 全部手写并解析；另附 nextpnr 实际支持的子集文件（见下文坑 4）。
- **流程**：`yosys synth_ecp5` → `nextpnr-ecp5 --sdc --freq --report --detailed-timing-report`，一条命令跑 5 个变体。
- **STA 分析**：Fmax、setup slack、关键路径逐级时延拆分（logic/routing/clk-to-q）、按终点/驱动分组的瓶颈 Top-N、自动生成的"哪几条路径卡住频率"结论。
- **时序收敛实验**：3 种手段（重结合 / 流水 / 时序驱动布局开关）的对照，全部量化。
- **逻辑等价检查**：流水等价形式化证明（PROVEN）+ 重结合的形式化尝试（未收敛，如实报告）+ 2160 次逐拍仿真比对。
- **功耗**：VCD 翻转率提取 + 相对动态功耗模型 + 三场景对照。
- **测试与 CI**：266 项 pytest；GitHub Actions（Ubuntu + Windows）。

**没有做**：签核 STA、DRC/LVS、UPF/CPF、多物理角 PVT、真实工艺库、ASIC 综合、时钟树综合（CTS）、扫描链/DFT、真实芯片流片。

---

## 开发过程中被测试抓出来的真问题

这些都是**真的踩到并修掉**的，多数由测试或工具报错抓出来：

1. **测试平台竞态**：`din` 在时钟上升沿**同一时刻**赋值，DUT 的移位寄存器与 testbench 的参考历史寄存器可能采到不同的值，导致 720 次比对里 **674 次失配**、数值各自都"看起来合理"。修法：所有激励改为在上升沿后 `#1` 再变，彻底消除竞态。
2. **`"" in " \t\r"` 恒为 True**：SDC 解析器的空白跳过循环用**字符串**做成员判断，输入结尾 `peek()` 返回 `""`，空串是任何字符串的子串 → `self.advance()` 空转 → **整个测试套件无限挂起**（只输出 "collected" 就再无输出）。而 CLI 路径正常，因为磁盘上的 SDC 文件结尾有换行。修法：`_WS`/`_BARE_STOP` 改为 `frozenset`，并给循环加 `at_end()` 保护。
3. **`cmd.exe` 吃掉非 ASCII 路径**：本机仓库路径含中文字符，`cmd /c` 会按控制台代码页重编码命令行，yosys 收到乱码路径后报"文件不存在"。修法：`tclab.runner` 把所有参数改写成相对工作目录的路径，并**拒绝**把非 ASCII 绝对路径传给工具。
4. **nextpnr 的 SDC 支持远小于文档暗示**：`create_generated_clock` / `set_input_delay` / `set_output_delay` / `set_max_delay` / `set_multicycle_path` / `set_clock_groups` **全部报 `Unsupported SDC command`**，`set_false_path` 要求单实体 `-from`/`-to`；实测只有 `create_clock` 被接受。修法：拆成两份文件——完整的 `constr/fir_block.sdc`（约束意图，供解析器与真实 STA 工具用）+ `constr/fir_block_nextpnr.sdc`（nextpnr 实际消费的子集），并在 README 写明哪些命令 nextpnr 根本不认。
5. **`sat -seq` 从"任意状态"而非复位态出发**：流水等价证明第一次跑出 **`model found: FAIL!`**——命题其实是真的，但初始状态未约束，延迟链与流水寄存器互不相关。修法：加 `-set-init-zero` 把初态钉在设计的复位值上，1.4 秒内证明成功。
6. **Windows 上超时杀不掉孙进程**：命令经 `cmd /c tools\oss.bat` 调用，真正的工具是**孙进程**。`subprocess.run(timeout=)` 只杀 `cmd.exe`，yosys 继续持有继承的管道 → `communicate()` 永不返回 → **超时静默退化成无限挂起**（实测该步骤挂起后始终不返回，只能手工杀进程）。修法：改用 `Popen` + `taskkill /F /T` 杀掉整棵进程树，超时从"永不返回"变成 20.4 秒准时返回。
7. **测试 monkeypatch 打错了对象**：测试 patch 了 `tclab.runner.run_tool`，但 `flow.py` 是 `from .runner import run_tool` 直接绑定，于是测试**真的去调 yosys/nextpnr 跑综合布局**，套件因此极慢。修法：改为 patch `tclab.flow.run_tool`。
8. **PowerShell 的 `utf8` 带 BOM**：`Set-Content -Encoding utf8` 会写入 UTF-8 BOM，TCL 解释器把 BOM 当成命令名的一部分，报 `invalid command name "ï»¿puts"`。修法：所有 TCL/Verilog/Python 源文件一律无 BOM 写入（CI 里有一条专门检查所有源文件无 BOM 且为 UTF-8）。
9. **VCD 的 `$dumpvars` 被当成"参数"吞掉**：解析器把 `$dumpvars` 到 `$end` 之间的内容整体丢弃，于是**每个信号的初值都丢了**，导致每个信号的**第一次真实翻转**被算漏（clk 从 9 次变 8 次）。修法：`$dumpvars`/`$dumpon` 体按"初始条件"读入——记录为基线但**不计入翻转**。
10. **`set` 变量在解析时不可见**：原实现先解析完所有命令、再统一求值，于是 `set P 10` 之后的 `$P` 一律报"未定义变量"。修法：`set` 在解析阶段就地求值。
11. **同名内部线被误判为等价**：`equiv_make` 按名字配对，gold 的组合 `n1` 与 gate 的**寄存后** `n1` 被当成一对，产生 518 个**永远证不出来**的 `$equiv` 义务。修法：改用 `miter`+`sat` 的有界证明表述流水等价，并在 `flow/lec_pipe_equiv.ys` 注释里写明这条路的坑。
12. **`float("NaN")` 能解析成功**：时序报告里的 `"NaN"` 曾被原样读入，会向上污染所有 slack 计算。修法：`_as_float` 增加 `math.isfinite` 检查，非有限值退化为默认值。

---

## 边界与已知限制

**没做什么，以及为什么**：

1. **不是 ASIC 后端**。目标是 Lattice ECP5 FPGA。没有标准单元、没有 PDK、没有流片，FPGA 的 LUT/FF/DSP/BRAM 资源计数和 ASIC 的 mm²/门数没有可比性。
2. **没有工艺库，因此没有真正的 STA 与功耗**。没有 `.lib`/`.db`/SPEF，没有 OCV/AOCV 降额、没有串扰、没有时钟树。功耗只有**无量纲相对值**，绝不能读成 mW。
3. **没有 hold 分析**。nextpnr 的 ECP5 流程只做 max-delay（建立时间）分析，报告里**没有 hold 要求可比**。本项目如实把 hold 标为 `not analysed`，同时保留报告里的 min-delay 到达值供人工查看，而**不是编一个 hold slack 出来**。
4. **没有 DRC / LVS / ERC / antenna**。这里的"PV 相邻能力"仅指**逻辑等价检查（LEC）**，完整物理验证未涉及。
5. **没有多电压域**。没有 UPF/CPF、没有电源域、没有隔离单元与电平转换器，也没有任何低功耗结构（时钟门控单元、电源门控）真实插入——功耗对比改的是**活动率**，不是门控单元。
6. **时序尚未收敛**。最接近的 `pipe` 变体在 100 MHz 下仍有 **−0.570 ns** setup slack；项目展示的是**收敛方法**，不是一个已收敛的设计。
7. **重结合的全宽形式化等价未证明**。已如实标注为未收敛，并用 2160 次逐拍仿真比对覆盖；不要把它读成"已形式化证明"。
8. **SDC 只覆盖 block 级、单一时钟对**，且 `clk_div2` 域的跨域交接是**捕获式总线、没有握手也没有同步器**，本项目不实现也不验证 CDC 同步器。
9. **无 I/O 引脚约束（LPF）**。block 级流程用 `--lpf-allow-unconstrained`，因此 I/O 路径是工具推导的默认约束，不代表任何真实板级接口预算。

---

## 目录结构

```
timing-closure-lab/
├── README.md
├── LICENSE                       MIT
├── pyproject.toml                无第三方运行时依赖；pytest 配置
├── .github/workflows/ci.yml      Ubuntu + Windows 单元测试 + 静态检查
│
├── rtl/                          设计
│   ├── fir_coeffs.vh             16 抽头 Hamming 窗 sinc 系数（Q1.14）
│   ├── fir_sum.v                 求和网络：fir_sum_chain / fir_sum_tree
│   ├── fir_core.v                FIR 核：延迟线 + 乘法阵列 + 求和网络 + 输出寄存器
│   ├── fir_top.v                 三个可互换顶层（chain / tree / pipe）
│   ├── fir_block_top.v           block 级顶层：主时钟 + 分频生成时钟 + 慢域捕获
│   └── lec_pipe_wrapper.v        形式等价检查专用 harness（不参与综合）
│
├── constr/
│   ├── fir_block.sdc             完整 SDC（8 类命令）
│   └── fir_block_nextpnr.sdc     nextpnr 实际支持的子集（仅 create_clock）
│
├── flow/                         TCL / yosys 脚本
│   ├── lec_pipe_equiv.ys         流水等价（有界序列证明，PROVEN）
│   ├── lec_sum_equiv.ys          重结合全宽 CEC（未收敛，如实标注）
│   └── lec_sum_equiv_scaling_probe.ys  缩放探测脚本
│
├── sim/
│   ├── tb_fir.v                  自检 testbench（对比行为级黄金模型）
│   └── tb_activity.v             VCD 活动率 testbench（full / gated / idle）
│
├── tclab/                        Python 分析层（模块化，无巨型文件）
│   ├── sdc.py                    TCL/SDC 子集解析器（花括号/命令替换/变量/续行/行号报错）
│   ├── report_parse.py           nextpnr JSON 报告读取（含退化输入容错）
│   ├── sta.py                    Fmax、slack、关键路径、瓶颈 Top-N、结论生成
│   ├── power.py                  VCD 解析 + 相对动态功耗模型
│   ├── lec.py                    等价检查驱动与结论分类
│   ├── flow.py                   流程编排与对照表
│   ├── runner.py                 工具定位、调用、超时与进程树终止
│   └── cli.py / __main__.py      命令行入口
│
├── tests/                        266 项 pytest
│   ├── conftest.py               共享 fixture + EDA 可用性探测（自动跳过）
│   ├── test_sdc.py               SDC 解析（含错误语法带行号）
│   ├── test_report_parse.py      报告解析（含空报告 / 缺字段 / NaN）
│   ├── test_sta.py               Fmax / slack / 关键路径 / 瓶颈
│   ├── test_power.py             翻转率统计与功耗模型
│   ├── test_flow.py              编排、对照表算术、报告生成
│   ├── test_runner.py            工具定位、路径重写、错误路径
│   ├── test_regressions.py       12 类真实踩坑的回归测试
│   └── test_eda_integration.py   真实综合 / 布局布线 / 仿真 / 形式验证集成测试
│
├── tools/
│   ├── oss.bat / oss.sh          EDA 环境包装脚本（Windows / POSIX）
│   └── make_samples.py           从真实运行生成 samples/ 固定输入
│
├── samples/                      CI 用的真实工具输出（裁剪，非手写）
│   ├── nextpnr_report_full.json  baseline 变体的完整 nextpnr 报告
│   ├── nextpnr_report_sample.json  裁剪版
│   ├── activity_sample.vcd       真实 VCD 片段
│   └── README.md                 出处说明
│
└── reports/                      真实运行产物（timing_summary.json / sta_report.md 入库）
```

---

## 测试与 CI

### 测试

```bash
py -3.12 -m pytest tests -q        # 266 passed
```

| 覆盖面 | 说明 |
|---|---|
| SDC 解析 | 正常命令、花括号分组（含嵌套）、命令替换、变量 `set`/`$var`、续行、注释、分号；**错误语法一律带行号报错**（未闭合括号/引号、未知命令、未知选项、缺值、非数值、非正周期） |
| 时序报告解析 | 真实报告、**空报告**、缺整节、`null` 节、类型错误、列表里的 `null` 条目、`NaN`、非 JSON、不存在的文件 |
| Fmax / slack | 独立手算校验，含零/负延迟与除零退化 |
| 关键路径提取 | 步骤求和与工具 Fmax 的一致性、时延拆分、起点终点归一化、布线/逻辑主导判定 |
| 翻转率统计 | 逐位翻转、总线位翻转、x/z 不计入、`$dumpoff` 语义、时长/活动因子 |
| 功耗模型 | 手算校验的分类拆分、零活动、权重可配置、比值与差值 |
| RTL 自检 | 用 `iverilog` 真跑三个变体的 testbench，各 720 次比对 |
| 流程编排错误路径 | 工具缺失、非零退出、超时、LEC 四种结论分类、失败变体不参与对照 |

**「注入已知问题 → 断言被抓出来」的测试**（`tests/test_regressions.py`，12 类，直接对应上文的真实踩坑）：测试平台竞态、空串成员判断导致的无限循环、`cmd.exe` 非 ASCII 路径、nextpnr SDC 子集边界、`sat` 初态未约束、进程树超时、monkeypatch 打错对象、UTF-8 BOM、VCD `$dumpvars` 丢失、`set` 求值时机、同名内部线误判等价、`NaN` 污染。

### CI

`.github/workflows/ci.yml`，两个 job：

- **`unit-tests`**：Ubuntu + Windows 矩阵，Python 3.12，跑 `pytest tests -v -ra`。
- **`static-checks`**：字节编译 + **全仓源文件 UTF-8 无 BOM 检查** + 用仓库内 `samples/` 的真实报告跑通 CLI。

**如实说明 CI 的降级范围**：GitHub runner 上**没有 OSS CAD Suite**。workflow 会**尽力**在 Ubuntu 上 `apt-get install yosys iverilog nextpnr-ecp5`（`continue-on-error: true`），但**不依赖它成功**。

- **CI 保证的**：解析层与算法层全部通过（SDC 解析、报告解析、STA 计算、翻转率与功耗模型、流程编排错误路径），使用仓库内提交的**真实工具输出**作为固定输入。
- **CI 不保证的**：真实的综合、布局布线、时序收敛实验与形式等价检查。这些测试由 `tests/conftest.py` 在**运行时探测工具可用性**决定是否执行，不可用则**跳过**（不是通过）。
- 每个 job 都会先跑 `python -m tclab env` 打印**实际找到了哪些工具**，日志里能直接看出本次运行跳过多少。

**完整流程必须本地装 OSS CAD Suite。CI 只验证解析与算法层**——这是有意的降级，不会用跳过伪装成绿灯。

---

## License

MIT，见 [LICENSE](LICENSE)。
