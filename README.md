# Agent Chronicle

AI 에이전트의 공개 작업 기록을 **로컬 Markdown 원장**으로 보존하고, 하루 단위 정리와 검색으로 다시 찾는 CLI입니다. Obsidian은 선택 사항이며 일반 로컬 폴더에서도 동작합니다.

> **상태: 개발 프리뷰 — 운영 승인/정식 릴리스가 아닙니다.**
> 상주 서비스의 로그인 자동 시작 설정, 준비 완료 통지, 장애 재시작 및 이어 수집을 구현하고 합성 데이터로 실제 systemd 동작을 시험했습니다. 다만 실제 사용자 수집을 활성화한 상태나 기본판 전체의 독립 QA 인수 완료는 아닙니다. 이 저장소를 “전체 구현 완료”로 간주하지 마세요. 실제 사용자 로그를 연결하기 전 아래 제한을 확인하세요.

## 현재 범위

- 저장할 폴더 지정. 경로를 생략하면 질문하며 기본값은 `~/Documents/Obsidian/AgentHistory`.
- 명시적으로 등록한 **Pi session-v3 JSONL** 파일/디렉터리 수집.
- Linux systemd 사용자 서비스의 로그인 자동 시작·장애 재시작·중단 후 이어 수집.
- 선택적으로 foreground 수집, 실행 중 추가 레코드 반영, Ctrl+C/SIGTERM 종료 및 재개.
- 공개 대화·도구 입력/결과·오류·제공된 텍스트 변경 내용의 기록.
- 이벤트 Markdown, 문서 리비전·체크포인트, 사실 기반 일일 노트.
- 한글 키워드와 프로젝트·날짜·작성자 필터 검색. 결과에 Vault 상대 Markdown 위치 제공.
- Markdown에서 검색/수집 투영 재구축.
- 미완성 JSONL/UTF-8은 보류하고, 완결된 손상 레코드는 오류로 표시.
- 기본 수집·일일 정리에서 고급 지식/주간 검토 자동 경로 분리.

숨겨진 추론, 홈 전체 검색, credential 수집을 목적으로 하지 않습니다. 패턴 기반 마스킹은 모든 비밀정보를 탐지한다는 보장이 아닙니다.

## 요구 환경

- Python **3.12 이상**. 현재 재현에 사용한 환경은 Linux / Python 3.14.4입니다.
- `PyYAML>=6.0,<7`.
- 단일 호스트의 로컬 파일시스템과 단일 writer.

다른 OS, Python 3.12의 별도 의존성 환경, 재부팅·전원 손실까지 검증했다고 주장하지 않습니다. 아래 명령은 새 패키지를 설치하지 않는 소스 실행 방식입니다.

```bash
git clone https://github.com/DeclanJeon/agent-chronicle.git
cd agent-chronicle

python3 -c "import yaml; print(yaml.__version__)"
export PYTHONPATH="$PWD/src"
python3 -m chronicle --help
```

PyYAML이 없다면 환경 준비가 필요합니다. 이 프로젝트의 에이전트 작업에서는 사용자 승인 없이 패키지를 설치하지 않았습니다.

## 상주형 설치: 처음 한 번 설정

위 소스 실행 준비 후 **계속 보관할 설치 폴더**에서 실행합니다. 저장 위치와 읽어도 되는 Pi 로그 폴더는 직접 지정합니다. 의존성 설치와 실제 로그 수집에 앞서 아래 개발 프리뷰 제한을 확인하세요.

```bash
export PYTHONPATH="$PWD/src"
# 터미널에서 저장 위치를 질문합니다. 일반 폴더도 가능합니다.
python3 -m chronicle init --timezone Asia/Seoul
python3 -m chronicle sources enable \
  --source pi-jsonl-v3 --root "/선택한/Pi/세션폴더" --project my-work

python3 -m chronicle service install --preview
# 확인 후: 즉시 시작 + 사용자 로그인 시 자동 시작 설정
python3 -m chronicle service install --confirm
python3 -m chronicle service status
```

- `--root`로 등록한 범위의 새 JSONL 세션과 추가 내용을 지속 수집합니다. 로그가 남아 있으면 중지 중 쌓인 내용도 재시작 후 따라잡습니다.
- 서비스는 설치 시 Python 실행 파일·소스 위치·config/state/cache 위치를 고정합니다. 이후 터미널을 켜거나 `PYTHONPATH`를 다시 export할 필요 없이 실행됩니다. **설치 폴더/Python 환경을 삭제하거나 이동하지 마세요.** 관리 CLI를 새 터미널에서 쓸 때에는 설치 폴더에서 위 `PYTHONPATH`를 다시 설정합니다.
- `Type=notify` 준비 완료 뒤 시작 성공을 확인합니다. `status`의 프로세스 상태, `health`, PID, 재시작 횟수는 서로 다른 정보입니다. 입력 오류는 degraded로 표시하고 재시도하며 손상 줄을 조용히 건너뛰지 않습니다.
- 프로세스가 예기치 않게 끝나면 5초 간격으로 다시 시작합니다. **명시적인 stop/remove에는 자동 재시작하지 않습니다.** `stop`은 로그인 자동 시작 설정을 보존하고, `remove`는 해제합니다.
- sudo/cron/별도 상주 셸은 필요 없습니다. 사용자 systemd manager가 필요하며 로그인 전 부팅 실행·로그아웃 후 보장은 하지 않습니다. linger를 자동 변경하지 않습니다.

```bash
python3 -m chronicle service status
journalctl --user -u agent-chronicle.service -f
python3 -m chronicle service restart --confirm
python3 -m chronicle service stop --confirm
python3 -m chronicle service remove --confirm  # 자동 시작 해제, 기록/설정은 보존
```

이미 수집기가 실행 중이면 중복 실행하지 않습니다. 다른 profile/runtime의 기존 unit은 덮어쓰거나 중지하지 않습니다. 실패 시 `service status`와 journal을 확인하세요. 상세 동작·운영 한계는 [상주 서비스 안내](docs/resident-service.md)에 있습니다.

## 빠른 시작: 합성 데이터로 시험

먼저 실제 세션 대신 새 시험 폴더와 합성 JSONL을 사용하세요. 아래 환경변수는 config/state/cache도 시험 폴더로 격리합니다.

```bash
export PYTHONPATH="$PWD/src"
DEMO=$(mktemp -d)
export CHRONICLE_CONFIG_DIR="$DEMO/config"
export CHRONICLE_STATE_DIR="$DEMO/state"
export CHRONICLE_CACHE_DIR="$DEMO/cache"
mkdir -p "$DEMO/input"

python3 - "$DEMO/input/session.jsonl" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

now = datetime.now(timezone.utc)
rows = [
    {"type": "session", "version": 3, "id": "demo-session",
     "timestamp": now.isoformat(), "cwd": "/synthetic/demo"},
    {"type": "message", "id": "demo-message-1", "parentId": None,
     "timestamp": now.isoformat(),
     "message": {"role": "user", "content": "합성기록: 첫 작업을 남깁니다.",
                 "timestamp": int(now.timestamp() * 1000)}},
]
Path(sys.argv[1]).write_text(
    "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
    encoding="utf-8",
)
PY

python3 -m chronicle init --vault "$DEMO/Vault" --timezone Asia/Seoul
python3 -m chronicle sources enable \
  --source pi-jsonl-v3 --file "$DEMO/input/session.jsonl" \
  --project demo-history --title "합성 Pi 세션"

python3 -m chronicle collect --once
python3 -m chronicle status
python3 -m chronicle daily --date "$(TZ=Asia/Seoul date +%F)"
python3 -m chronicle search --query "합성기록" --project demo-history
```

### 계속 수집하고 정지하기

같은 시험 환경에서 다음 명령을 실행하면 foreground로 수집합니다.

```bash
python3 -m chronicle collect --interval-seconds 1
```

**Ctrl+C**로 정지합니다. 같은 설정과 같은 소스로 다시 실행하면 이어 수집합니다. 이 foreground 명령 자체는 서비스 등록이나 자동 시작 설정을 하지 않습니다. 상주 운영은 위 `service install --confirm`을 사용하세요.

### 주요 명령

```bash
python3 -m chronicle sources list
python3 -m chronicle status
python3 -m chronicle doctor
python3 -m chronicle rebuild
python3 -m chronicle search --query "키워드" --project demo-history
python3 -m chronicle search --help
```

`rebuild`는 저장된 Markdown에서 투영을 재생성합니다. 손상된 원본 JSONL의 잃어버린 내용을 복구하거나 추정하지 않습니다.

## 데이터 위치와 저장 원칙

| 위치 | 역할 |
|---|---|
| `80_Events/` | 이벤트 Markdown 원장 |
| `50_Daily/` | 날짜별 일일 정리 |
| `90_System/` | 체크포인트·일일 cursor 등 내부 기록 |
| Vault 밖 config/state/cache | 설치 설정과 재구축 가능한 로컬 투영 |

- Markdown과 과거 리비전·체크포인트·멱등 키를 보존합니다.
- 검색의 `event`와 `artifact_revision` 행은 같은 이벤트를 가리킬 수 있습니다. 행 수를 수집 이벤트 수로 해석하지 마세요.
- 원본 세션은 읽기 전용 입력이며, 마스킹된 출력과 원본 자체는 다릅니다.
- 시험 환경에서 실제 사용 환경으로 바꾸려면 명시한 디렉터리·권한·수집 범위를 먼저 확인하세요.

## 오류·보안 제한

- 검증 오류는 주로 exit 2, 일부 수집 실패는 exit 1로 보고됩니다.
- **exit 0은 전체 기록 완료를 뜻하지 않습니다.** `collect`가 `pending`일 수 있고, `status`는 실패 상태를 조회하면서 0으로 끝날 수 있습니다.
- cursor는 각 디렉터리를 no-follow FD로 열고 고정된 부모 FD에서 제한 읽기·파일 fsync·원자 교체·디렉터리 fsync를 수행합니다. 잘못된 경로/미래·손상 cursor는 `doctor`에서 `attention`/exit1로 표시합니다. `doctor`는 cursor를 자동 초기화하거나 일일 노트를 만들지 않습니다.
- foreground의 `ready`는 시작 상태이며, 유지 작업 실패는 별도 `health=degraded`로 표시합니다. `healthy`는 관측된 저장소 검사 결과이지 모든 일일 내용·지식 기능의 완료 승인이나 모든 OS 경쟁/전원손실의 보장이 아닙니다.
- 손상된 완결 JSONL은 원본 제공자·백업·재내보내기로 복구해야 합니다. 손상 줄을 조용히 건너뛰거나 내용을 만들어 채우지 않습니다.
- Drive/동기화·네트워크 마운트 writer, 외부 AI/embedding/자동 export는 지원 범위가 아닙니다. 상주 설치는 현재 Linux systemd 사용자 서비스만 지원합니다. 다른 OS 및 실제 로그아웃/로그인·재부팅 시험은 NOT_RUN입니다.
- `ask`의 생성 답변은 미지원/차단 상태입니다. 고급 지식·승인·관계·review/context CLI는 실험 기능이며 기본 기록판 전체 수락과 별개입니다.

## 초기 공개본에서 확인한 결과 — `8a03414`

2026-11-07, 게시용으로 분리한 동일 소스 사본에서 실행했습니다.

- 전체 unittest **134 PASS**.
- basedpyright **0 errors / 0 warnings / 0 notes**.
- 이 README의 합성 JSONL 빠른 시작을 실제 실행: 설정→등록→수집→일일 정리→한글 검색 **PASS**.
- 위 결과는 기본판 안전 수정 완료나 독립 QA PASS가 아닙니다. pytest/Ruff/운영 데이터/OS 전원 손실 결과로 승계하지 않습니다.

자세한 범위는 [게시본 검증 기록](docs/verification.md)을 참조하세요.

## cursor·doctor 수정 검증

- 관련 회귀 **21 PASS**. 기존 커밋에 새 안전 회귀만 적용한 음성 대조에서 4개 실패를 확인했습니다.
- 전체 unittest **148 PASS**.
- 전체 pytest **148 PASS + 31 subtests PASS**.
- basedpyright **0 errors / 0 warnings / 0 notes**, 변경 Python 파일 no-excuse **0 violations**, compileall 및 변경 diff 공백 검사 PASS.
- 실제 CLI: 미래 cursor에서 doctor attention/exit1·foreground degraded, 정상 합성 상태에서 doctor healthy/exit0·foreground healthy. 두 수집기는 정상 종료·회수했습니다.
- 작성자 검증이며 독립 승인과 다릅니다. 작성자 Markdown 렌더링·일일 오류 요약 등 별도 보강 항목은 이번 두 수정의 완료로 승계하지 않습니다.

## 상주형 수정 검증 — resident-recording-r1

- 관련 회귀 **28 PASS**, 전체 pytest **169 PASS + 31 subtests PASS**.
- basedpyright **0 errors / 0 warnings / 0 notes**, 변경/관련 Python12파일 no-excuse **0 violations**, compileall/diff 공백 검사 PASS.
- 실제 systemd 합성 시험: 로그인용 enable 링크, notify 준비 완료, 새 세션/append, SIGKILL 및 예기치 않은 정상 종료 후 자동 재시작, 입력 오류 degraded/복구 healthy, 명시 stop 유지, 누적 기록 재개, replay 무중복, 제거·데이터 보존 PASS.
- 설치 소스/설정 경로에 한글·공백·`%`·`$`·따옴표를 넣은 실제 서비스 시험도 PASS. 임시 서비스는 모두 제거했습니다.
- `doctor`가 신선한 state에 lock을 생성하던 독립 QA finding은 별도 커밋 `768ab09`에서 수정했습니다. 해당 고정본의 전체 pytest151+31 PASS이며 원래 CHANGES_REQUIRED를 독립 PASS로 바꿔 주장하지 않습니다.
- 작성자 시험 결과입니다. 실제 로그아웃/로그인·재부팅·전원손실 및 실제 사용자 로그의 지속 운영은 NOT_RUN, 최종 독립 인수는 별도입니다.

## 개발·검증

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m pytest
basedpyright
python3 -m compileall -q src tests
```

사용 가능한 도구만 실행하며, 없는 의존성·Ruff·OS 전원손실 검사를 실행했다고 기록하지 않습니다. 작성자 시험, 통합, 독립 QA, 사용자 인수는 구분합니다. 소스와 테스트·문서만 버전 관리하고 실제 로그/Vault/config/state/cache·인증정보·진단 압축은 올리지 않습니다.

## 다음 전달 기준

1. FD 기준 cursor 경로 경계와 안전한 저장: 구현·작성자 회귀 완료, 독립 검토 대상.
2. 일일 정리 실패가 드러나는 상태/doctor: 구현·작성자 회귀 완료, 독립 검토 대상.
3. 기본 기록판 시작→추가 수집→일일→검색→종료/재시작→복구의 동일 고정본 검증.
4. 같은 코드의 독립 QA와 한국어 실행 안내.

GitHub 업로드는 개발 코드 전달이며 위 완료 조건을 대신하지 않습니다.
