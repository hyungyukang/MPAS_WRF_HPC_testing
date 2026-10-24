# CLAUDE.md — PerfSonde 프로젝트 컨텍스트

PerfSonde: HPC performance probe for weather codes (MPAS-A, WRF). 이전 이름은 wxperf.

Claude Code 가 이 저장소에서 작업할 때 먼저 읽는 파일입니다. 사용자와는 한국어로 대화합니다.

## 목표
MPAS-A 와 WRF 의 성능을 빌드 축(컴파일러, 최적화 플래그, OpenMP, netCDF vs PnetCDF,
라이브러리 버전)과 실행 축(노드 수, MPI×OpenMP, 반복)에 대해 여러 HPC 에서 자동으로
측정·비교한다. 최종 대상은 오프라인 새 HPC 이고, 인터넷이 되는 **Frontier(OLCF)** 에서 먼저 돌려 본다.
사용자는 두 모델 모두 gnu 로 수동 빌드에 성공한 경험이 있다. 스케줄러는 Slurm 만 지원하면 된다.

## 현재 단계 (Frontier 에서 작업 재개)
1. 로그인 노드에서 `./perfsonde discover --name frontier` → `config/sites/frontier/` 생성.
   `discovery.md` 를 사용자와 함께 검토하고, 이상한 값(모듈 선택, 경로, 코어 수)을 바로잡는다.
   - Frontier 사전 지식(확인 필요): 노드 64코어·SMT2 지만 기본 core specialization 으로 56코어 사용,
     파티션 `batch`/`extended`, `-A <project>` 필수, HPE Cray PE(PrgEnv-gnu, cray-mpich,
     cray-hdf5, cray-netcdf, cray-parallel-netcdf, 래퍼 ftn/cc/CC, 경로 `$NETCDF_DIR`/`$PNETCDF_DIR`).
   - 빌드 위치(`build.root`)는 홈이 아닌 프로젝트/스크래치 파일시스템으로.
2. `.venv` 에 reframe-hpc 설치 (Python ≥3.9 필요 → 필요하면 `module load cray-python`).
3. `./perfsonde selftest` → `./perfsonde build list -v` → 빌드 1개만 `--only` 로 시험 → 성공하면 전체.
4. MPAS·WRF 테스트 케이스(경로·크기)를 사용자에게 받아 `matrix.yaml` 작성 → `./perfsonde dry` → 1노드 1조합 실행.

## 구조와 흐름
- **사이트 프로필**: `config/sites/<name>/{site,builds,matrix}.yaml` (+ `discovery.md/json`).
  선택 규칙은 `lib/perfsonde/site.py`: `--site`/`PERFSONDE_SITE_NAME` → hostname 정규식 → 유일한 프로필.
  경로에 `{site}`, `$VAR`, `~` 사용 가능. 기본 작업 디렉터리 `work/<site>/` (git 제외).
  `config/sites/newhpc/` 는 손으로 쓴 오프라인 새 HPC 예시.
- **discover** (`tools/discover.py`, 표준 라이브러리만, Python 3.6+): 하위 bash 에서
  `module reset|purge` → 컴파일러 로드 → `module -t avail` 재조회(계층) → MPI/HDF5/netCDF/PnetCDF 선택
  → 로드 → `module -t list` 로 버전 고정 → `module display` 의 setenv / `*-config --prefix` / PATH 의 bin
  상위 경로 중 헤더가 실제로 있는 것을 prefix 로 채택 → 래퍼 감지(Cray: ftn/cc/CC).
  Slurm: sinfo, `scontrol show node`(CPUEfctv/CoreSpecCount), `scontrol show config`(accounting),
  sacctmgr. `--probe-job` 은 sbatch --wait 로 nproc 실측.
- **빌드** (`build/build_matrix.py`, `./perfsonde build ...`): `builds.yaml` 축의 데카르트 곱마다
  `work/<site>/builds/<name>/` (소스 복사, `env.sh`, `build.sh`, `build.json`, `bin/`, 로그). 성공 시 `BUILD_OK`.
  - 툴체인의 `mpas.make_overrides`(FC_PARALLEL 등)와 opt 축의 플래그는 재귀 병합된다.
  - MPAS: Makefile 에 `perfsonde_<target>` 타깃 추가 (`patch_mpas_makefile`).
  - WRF: `wrf_configure_menu()` 가 Config.pl 번호 규칙 재현 → `printf "N\n1\n" | ./configure`
    → `# Settings for ...` 로 검증 → `configure_overrides` 를 sed 로 적용.
  - env 값은 bash 식이며 참조 순서대로 정렬된다(`ordered_env`). `null` 은 unset.
- **실행** (`./perfsonde run mpas|wrf|osu`): ReFrame, `config/reframe_settings.py` 가 현재 사이트를 읽음.
  공통 로직은 `lib/perfsonde/base.py` (`SondeRunBase`). ReFrame 모듈 연동은 끄고(`nomod`) job 이 `env.sh` 를 source.
  보조 도구 모듈은 `site.yaml` 의 `tools:` (metis, nccmp, darshan, osu).
- **분석**: `analysis/collect.py` → `runs.csv`, `summary.csv`; `analysis/plot.py` → `figures/`.

## 검증된 것 / 안 된 것
- 확인됨 (2코어 Ubuntu VM): WRF 메뉴 번호 재현(Config.pl 과 83개 일치), MPAS·WRF em_real 실제 빌드,
  discover 를 가짜 Lmod 트리(HPE Cray PE 형, Spack 계층형)와 가짜 Slurm 으로 실행,
  생성된 Cray 형 프로필로 MPAS·WRF 실제 빌드, ReFrame 4.10.4 로 17개 테스트 전체 경로(가짜 실행 파일),
  discover 의 Python 3.8 + PyYAML 없는 환경 동작, `--probe-job`(가짜 sbatch).
- 미확인: 실제 Frontier/새 HPC 의 Lmod 출력 세부, `module reset` 의 기본 모듈, cpe 버전 전환 방식,
  Environment Modules(Tcl) 시스템, 실제 Slurm 제출, osu 바이너리 위치, Darshan(`DARSHAN_LOGFILE`),
  실제 MPAS/WRF 케이스 실행.

## 작업 규칙
- YAML 의 TODO 값(소스 경로, 케이스, 계정 등)은 추측하지 말고 사용자에게 묻는다.
- discover 가 틀린 값을 내면 생성된 YAML 만 고치지 말고 `tools/discover.py` 의 규칙도 고쳐서
  다른 시스템에서도 맞게 한다. 실제 출력(`module -t avail` 등)을 `discovery.json` 에서 확인할 수 있다.
- 원본 케이스 디렉터리와 사용자 소스 트리는 절대 수정하지 않는다 (복사본에서만 작업).
- 파서 변경 시 `tests/data/` 샘플로 `./perfsonde selftest` 가 통과하는지 확인한다.
- 대량 job 제출 전에는 `./perfsonde list` 로 job 수를 사용자에게 보여 주고 확인받는다 (allocation 소모).
