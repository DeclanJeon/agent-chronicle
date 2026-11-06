# Agent Chronicle

AI 에이전트의 공개 작업 기록을 **로컬 Markdown 원장**으로 보존하고, 하루 단위 정리와 검색으로 다시 찾는 CLI입니다. Obsidian은 선택 사항이며 일반 로컬 폴더에서도 동작합니다.

> **상태: 개발 프리뷰 — 운영 승인/정식 릴리스가 아닙니다.**
> 기본 기록 흐름은 구현돼 있지만 cursor의 FD 기반 경로 고정·내구 저장과 doctor/health 보강은 아직 최종 고정본 및 독립 QA 인수 전입니다. 이 저장소를 “전체 구현 완료”로 간주하지 마세요. 실제 사용자 로그를 연결하기 전 아래 제한을 확인하세요.

## 현재 범위

- 저장할 폴더 지정. 경로를 생략하면 질문하며 기본값은 `~/Documents/Obsidian/AgentHistory`.
- 명시적으로 등록한 **Pi session-v3 JSONL** 파일/디렉터리 수집.
- foreground 수집, 실행 중 추가 레코드 반영, Ctrl+C/SIGTERM 종료 및 재개.
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

**Ctrl+C**로 정지합니다. 같은 설정과 같은 소스로 다시 실행하면 이어 수집합니다. 서비스 등록이나 자동 시작 설정은 하지 않습니다.

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
- 부모 symlink/future cursor의 기존 v012 누락 경로에는 검사 코드가 추가됐지만, FD 기준 no-follow I/O와 health/doctor 판정은 마무리 검증 대상입니다. `ready`/`healthy`만으로 일일 정리 완료를 단정하지 마세요.
- 손상된 완결 JSONL은 원본 제공자·백업·재내보내기로 복구해야 합니다. 손상 줄을 조용히 건너뛰거나 내용을 만들어 채우지 않습니다.
- Drive/동기화·네트워크 마운트 writer, 외부 AI/embedding/자동 export, 실제 서비스 설치·배포는 기본 사용 범위가 아닙니다.
- `ask`의 생성 답변은 미지원/차단 상태입니다. 고급 지식·승인·관계·review/context CLI는 실험 기능이며 기본 기록판 전체 수락과 별개입니다.

## 이번 게시본에서 확인한 결과

2026-11-07, 게시용으로 분리한 동일 소스 사본에서 실행했습니다.

- 전체 unittest **134 PASS**.
- basedpyright **0 errors / 0 warnings / 0 notes**.
- 이 README의 합성 JSONL 빠른 시작을 실제 실행: 설정→등록→수집→일일 정리→한글 검색 **PASS**.
- 위 결과는 기본판 안전 수정 완료나 독립 QA PASS가 아닙니다. pytest/Ruff/운영 데이터/OS 전원 손실 결과로 승계하지 않습니다.

자세한 범위는 [게시본 검증 기록](docs/verification.md)을 참조하세요.

## 개발·검증

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m pytest
basedpyright
python3 -m compileall -q src tests
```

사용 가능한 도구만 실행하며, 없는 의존성·Ruff·OS 전원손실 검사를 실행했다고 기록하지 않습니다. 작성자 시험, 통합, 독립 QA, 사용자 인수는 구분합니다. 소스와 테스트·문서만 버전 관리하고 실제 로그/Vault/config/state/cache·인증정보·진단 압축은 올리지 않습니다.

## 다음 전달 기준

1. FD 기준 cursor 경로 경계와 안전한 저장.
2. 일일 정리 실패가 드러나는 상태/doctor.
3. 기본 기록판 시작→추가 수집→일일→검색→종료/재시작→복구의 동일 고정본 검증.
4. 같은 코드의 독립 QA와 한국어 실행 안내.

GitHub 업로드는 개발 코드 전달이며 위 완료 조건을 대신하지 않습니다.
