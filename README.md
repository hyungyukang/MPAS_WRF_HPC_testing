# PerfSonde — HPC performance probe for weather codes

**PerfSonde** 는 기상 코드(MPAS-A, WRF)의 HPC 성능을 자동으로 측정·비교하는 도구입니다 (ReFrame 기반).
이름은 라디오존데(radiosonde)에서 왔습니다. 존데가 모르는 대기 속으로 올라가 연직 프로파일을
받아 오듯, PerfSonde 는 처음 보는 HPC 에 들어가 환경을 탐지(`discover`)하고 성능 프로파일을 가져옵니다.

컴파일러, 최적화 플래그, OpenMP 유무, I/O 라이브러리(빌드 축)와
노드 수, MPI×OpenMP 배치, 반복 횟수(실행 축)의 조합을 자동으로 빌드·실행하고,
결과를 CSV와 그림으로 정리합니다. 여러 HPC(Slurm + Lmod/Environment Modules)에서
쓸 수 있도록 시스템별 값은 **사이트 프로필**에 모으고, 프로필은 그 시스템에서
`module list/avail/display` 와 Slurm 명령으로 **자동 생성**합니다.

```
perfsonde            실행 진입점 (모든 명령 앞에 --site NAME 가능)
tools/discover.py    시스템 탐지 → config/sites/<NAME>/ 생성
config/sites/<NAME>/ site.yaml    시스템 (파티션, 계정, 코어 수, 모듈 초기화, 보조 도구 모듈)
                     builds.yaml  빌드 매트릭스 (툴체인 × 플래그 × 병렬화 × I/O)
                     matrix.yaml  실행 매트릭스 (케이스, 노드, 레이아웃, 반복)
                     discovery.md 탐지 보고서 (무엇을 어디서 찾았는지, 경고, TODO)
config/sites/newhpc/ 손으로 쓴 예시 프로필 (오프라인 새 HPC)
config/reframe_settings.py   현재 사이트의 site.yaml 을 읽는 ReFrame 설정
build/build_matrix.py        빌드 변형 생성·제출·상태 확인
tests/               mpas_atm.py wrf_em.py osu.py selftest.py   ReFrame 테스트
analysis/            collect.py (표준 라이브러리만)  plot.py (matplotlib)
lib/perfsonde/          공통 코드 (사이트 선택, 매트릭스 확장, 로그 파서, namelist 패처, ReFrame 베이스)
offline/             인터넷 없는 시스템용 wheel 반입 스크립트
work/<NAME>/         빌드·ReFrame·결과 (기본 위치, git 에서 제외)
```

## 빠른 시작 (예: Frontier)
```bash
git clone <this repo> perfsonde && cd perfsonde
module load cray-python                        # Python >= 3.9 (ReFrame 용)
python3 -m venv .venv && .venv/bin/pip install reframe-hpc matplotlib
./perfsonde discover --name frontier --account <PROJECT>   # → config/sites/frontier/
less config/sites/frontier/discovery.md        # 무엇을 찾았는지 확인, TODO 채우기
./perfsonde selftest && ./perfsonde build list -v
```

## 사이트 선택 규칙
1. `./perfsonde --site NAME ...` (또는 `PERFSONDE_SITE_NAME`)
2. 현재 hostname 이 어떤 프로필의 `system.hostnames` 정규식과 맞으면 그 프로필
3. 프로필이 하나뿐이면 그것

`./perfsonde sites` 로 목록과 현재 선택을 봅니다.

## 0. 설치

인터넷이 되는 시스템 (예: Frontier):
```bash
git clone <this repo> perfsonde && cd perfsonde
python3 -m venv .venv && .venv/bin/pip install reframe-hpc matplotlib
```
ReFrame 4.10 은 Python 3.9 이상이 필요합니다. 시스템 기본 python3 가 오래됐으면
`cray-python` 같은 Python 모듈을 먼저 로드하세요. `perfsonde` 가 `.venv` 를 자동으로 씁니다.

인터넷이 없는 시스템: 인터넷 되는 곳에서 `offline/download_wheels.sh`
(대상 Python 버전은 `PYVER=3.14`), 결과물과 저장소를 옮긴 뒤 `offline/install_reframe.sh <tar.gz>`.

## 1. 사이트 프로필 만들기 (시스템마다 한 번, 로그인 노드에서)

```bash
./perfsonde discover                    # 이름은 hostname 에서 추정 (login05.frontier... → frontier)
./perfsonde discover --name frontier --account cli115 --partition batch   # 직접 지정
./perfsonde discover --probe-job        # 1노드 job 으로 사용 가능 코어 수까지 실측 (큐 대기)
```
discover 는 표준 라이브러리만 쓰므로 venv 없이도 돌아갑니다. 하는 일:

| 항목 | 방법 |
|---|---|
| 모듈 시스템, 깨끗한 시작 | `module --version`, `module reset` 가 되면 그것, 아니면 `module purge` |
| GNU 컴파일러 | HPE Cray PE 면 `PrgEnv-gnu` + `gcc-native`/`gcc` 버전들, 아니면 `gcc/*` (기본값 + 최신, `--max-compilers`) |
| MPI·HDF5·netCDF·PnetCDF | 컴파일러를 로드한 하위 셸에서 다시 `module -t avail` (계층형 모듈 대응) → 선택 → 로드 → `module -t list` 로 **실제 로드된 버전 고정** |
| 라이브러리 경로 | `module display` 의 `setenv` 변수(`NETCDF_DIR` 등), `nc-config/nf-config/pnetcdf-config --prefix`, 모듈이 PATH 에 넣는 `bin/` 의 상위 디렉터리 순으로 후보를 만들고 `include/netcdf.h`·`netcdf.mod`·`pnetcdf.h` 가 실제로 있는 것만 채택 |
| 컴파일러 래퍼 | Cray PE 면 `ftn/cc/CC`, 아니면 `mpif90(mpifort)/mpicc/mpicxx` → MPAS `FC_PARALLEL` 등, WRF `DM_FC/DM_CC` 에 자동 반영 |
| 보조 도구 | metis(gpmetis), nccmp, darshan-runtime, osu-micro-benchmarks 모듈 |
| Slurm | `sinfo`(파티션, 기본 파티션), `scontrol show node`(소켓·코어·SMT, `CPUEfctv`/`CoreSpecCount` 로 core specialization 반영), `scontrol show config`(accounting → ReFrame 스케줄러), `sacctmgr`(내 계정) |
| 버전 비교 후보 | 설치된 다른 버전(netCDF, MPI, `cpe/*` 등)을 `builds.yaml` 에 주석 예시로 남김 |

생성 후 `config/sites/<NAME>/discovery.md` 를 읽고 YAML 의 `TODO` 를 채웁니다:
`builds.yaml` 의 `mpas.source`·`wrf.source`, `matrix.yaml` 의 `case_dir`·`graph_file`,
`site.yaml` 의 `build.root`(용량 큰 공유 파일시스템 권장).

```bash
./perfsonde selftest                    # ReFrame·설정·파서 점검 (수 초, Slurm 안 씀)
./perfsonde build list -v               # 만들어질 빌드 목록과 모듈·환경변수
```

## 2. 빌드

```bash
./perfsonde build build                     # site.yaml 의 mode(기본 sbatch)로 전체 제출
./perfsonde build build -m mpas --only O3   # 일부만
./perfsonde build status
./perfsonde build build --force --only wrf-gnu13-O2   # 다시 빌드
```
각 빌드는 `work/<site>/builds/<이름>/` 에 독립된 소스 복사본, `env.sh`(모듈·환경),
`build.sh`, 로그, `build_info.txt`(실제 로드된 모듈, 컴파일러·래퍼 버전, `ldd`), `flags.txt` 를 남깁니다.
실패하면 `BUILD_FAILED` 와 `compile.log`/`configure.log` 를 보세요.

- **MPAS**: Makefile 에 `perfsonde_<target>` 타깃을 추가하고 `make_overrides` 로 플래그·래퍼를 교체합니다.
  최신 MPAS 는 기본이 단정밀도일 수 있으니 비교 시 `make_args: ["PRECISION=double"]` 등으로 명시하세요.
- **WRF**: `arch/configure.defaults` 에서 configure 메뉴 번호를 계산하고(Config.pl 과 같은 규칙)
  비대화식으로 configure 한 뒤 `configure.wrf` 의 `# Settings for` 줄로 검증합니다.
  `configure_overrides` 는 해당 변수 줄을 교체합니다 (Cray: `DM_FC=ftn`, `DM_CC=cc`).
  `./compile` 에는 csh 가 필요합니다. PnetCDF 지원은 configure 시점의 `PNETCDF` 로 결정됩니다(io 축).
- 빌드는 `.git` 을 뺀 복사본에서 하므로 원본 트리에 외부 파일이 들어 있어야 합니다:
  WRF 4.6+ 의 `phys/physics_mmm`·`phys/noahmp` 서브모듈, MPAS v8+ 의 물리 테이블(MPAS-Data).

## 3. 실행

```bash
./perfsonde list mpas            # 매트릭스가 몇 개의 job 으로 펼쳐지는지
./perfsonde dry  mpas            # job 스크립트만 생성 (work/<site>/rfm/stage/... 에서 확인)
./perfsonde run  osu             # 인터커넥트 기준선
./perfsonde run  mpas            # 제출하고 완료까지 대기 (tmux 안에서 실행 권장)
./perfsonde run  wrf -n 'nodes=1'   # ReFrame 옵션 그대로 전달
```
- 불가능한 조합은 건너뜁니다 (코어 수 초과, OpenMP 없는 빌드에 threads>1).
- MPAS partition 파일은 없으면 `gpmetis` 로 만들어 `work/<site>/builds/mpas_partitions/` 에 캐시합니다.
- `namelist`, `file_patches`, `by_build_label` 은 실행 디렉터리의 **복사본만** 수정합니다.
- `verify` 를 설정하면 `nccmp` 로 기준 출력과 비교해 `identical` 열에 기록합니다.
- `darshan: true` 면 `LD_PRELOAD` 로 Darshan I/O 로그를 남깁니다.

## 4. 결과

```bash
./perfsonde report                       # collect.py + plot.py
./perfsonde report --sacct               # sacct 의 Elapsed/MaxRSS/에너지 추가
```
- `work/<site>/results/results.jsonl`: 실행 1건당 1줄 (사이트, 빌드 축 라벨, 모듈, 레이아웃, 성능값, job id, 노드)
- `runs.csv`, `summary.csv`: 반복의 중앙값·최소·최대·CV, 속도 향상, 병렬 효율
- `figures/*_scaling.png`, `figures/*_layouts_N<n>.png`

| 모델 | 지표 (초) | 출처 |
|---|---|---|
| MPAS-A | `total_time`, `time_integration`, `extra_timers` | `log.atmosphere.0000.out` 타이머 표 |
| WRF | `mean_step`, `median_step`, `total_main`, `total_write`, `total_input` | `rsl.error.0000` |
| 공통 | `wall` | job 안에서 실행 전후 시각 |
| OSU | 메시지 크기별 latency(us) / bandwidth(MB/s) | osu_latency, osu_bw, osu_allreduce |

## 권장 순서
1. `./perfsonde run osu` 로 네트워크 기준선
2. 1노드에서 모든 빌드 × 레이아웃 → 빠른 빌드 2~3개 선택
3. 선택한 빌드만 (`builds: ['mpas-gnu13-O3native-.*']`) 노드 수를 늘려 스케일링
4. 반복 3회 이상, CV 가 크면(>3%) 노드 상태나 I/O 간섭을 의심

## 검증 상태
- WRF configure 메뉴 번호 계산: 실제 WRF `Config.pl` 출력과 83개 항목 일치
- 실제 빌드 성공 (gfortran 13 + OpenMPI, 2코어 VM): MPAS-A, WRF em_real(dm+sm)
- discover: 가짜 Lmod 트리 두 종류(HPE Cray PE 형 계층, Spack 형 계층)와 가짜 Slurm
  (Frontier 형 core specialization, accounting 없음)에서 프로필 생성 확인,
  생성된 Cray 형 프로필로 MPAS·WRF 실제 빌드까지 확인 (ftn/cc 래퍼, `$NETCDF_DIR` 경로)
- ReFrame 4.10: MPAS·WRF·OSU 테스트 17개 전체 경로를 가짜 실행 파일로 확인, Slurm job 스크립트는 `--dry-run`
- **아직 실제 HPC 에서는 실행하지 않았습니다.** Environment Modules(Tcl) 시스템도 미검증.
