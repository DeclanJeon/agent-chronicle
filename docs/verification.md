# 검증 기록

아래 초기 게시 기록은 `8a03414` 당시의 이력이며, 최신 cursor/doctor 수정 결과는 문서 마지막에 분리해 기록합니다.

## 초기 공개본 `8a03414`

## 상태와 입력

- 2026-11-07의 개발 프리뷰. 정식 릴리스/기본판 독립 인수 완료가 아니다.
- 실행 중인 작성자의 제품 파일을 수정하지 않고 src/tests 및 pyproject.toml을 별도 사본으로 읽어 게시했다.
- 사본 생성 전후 원본 hash와 사본 hash를 대조해 동일함을 확인했다.
- 기존 README는 로컬 프로젝트 history에 원문 보존 후 사용자 요청에 따라 사용 안내로 갱신했다.
- 이 저장소에는 코드·합성 시험 입력·새 README/검증 문서만 포함한다. 실제 Vault/세션·config/state/cache·인증·진단 로그·원본 설계/이력 압축은 제외했다.

## 실제 실행

| 검사 | 결과 |
|---|---|
| `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 134 tests PASS, 103.931초 |
| `basedpyright` | 0 errors, 0 warnings, 0 notes |
| README 합성 JSONL quick start | exit0; 설정→소스 등록→1건 수집→일일1건→한글 검색/MD 상대 위치 확인 |
| compileall | src/tests 컴파일 PASS |
| 사본 무변경 및 원본과 대조 | 게시 준비 시 111 source/test/config 파일 hash 일치 |
| git diff --check | 기존 소스/테스트 5곳의 EOF 빈 줄 경고. 게시자가 제품 코드를 임의 정규화하지 않았으며 코드 품질 전체 PASS 아님 |
| 게시 파일 제한·비밀 패턴 검사 | allowlist 적용, 검사한 GitHub/AWS/Slack/OpenAI 토큰 패턴 일치0 |

환경: Linux, Python3.14.4, PyYAML6.0.3. 새 의존성은 설치하지 않았다. 비밀 패턴 검사는 전체 DLP/비밀 부재의 수학적 증명이 아니다. raw 로그·HOME/config/state/cache 및 진단 압축은 GitHub에 올리지 않는다.

## 남은 사항

- FD 고정 no-follow cursor I/O, durable write와 doctor/health의 최종 구현·고정본 회귀.
- 최신 전체 작업에 대한 Atlas 통합과 같은 hash의 Vera 독립 QA.
- 과거 별도 author 결과/설계 QA를 이 커밋의 독립 PASS로 승계하지 않는다.
- pytest/Ruff의 이번 게시본 재실행, 실제 사용자 로그, 서비스 설치, 외부 AI/Drive, OS/전원 손실은 이 기록에서 NOT_RUN이다.
- 게시 시점 source에는 테스트 모듈 분리 작업이 아직 포함되지 않았을 수 있으며, 현재 진행 중인 변경과 이 고정 사본은 다르다.

GitHub 게시와 테스트 통과는 위 미완료 항목의 종결을 대신하지 않는다.

## cursor-doctor-r1 수정 결과

범위는 cursor 경로 안전성과 doctor 정상 오표시 두 항목이다. 기존 위임 cycle 종료·활성 작성자0을 확인하고 이 한정 브랜치의 작성 책임을 Dots가 인수했다. 다른 런타임/모델 설정이나 사용자 데이터는 변경하지 않았다.

### 변경

- `daily_cursor_store.py`: 루트부터 NOFOLLOW 디렉터리 FD를 열고 고정 부모 FD에서 cursor를 읽고 쓴다. regular/owner/private/single-link/4KiB 검증, NONBLOCK leaf open, temp fsync→replace→directory fsync.
- `daily_schedule.py`: 같은 FD 저장소 수명 안에서 읽기/쓰기, 일일 게시와 cursor 갱신을 같은 writer lock으로 묶고, 진단에는 생성·수리 없는 공유 검사 사용.
- `command_collector.py`: 저장된 cursor도 실제 검사하고 실패는 attention/exit1 및 오류코드를 출력. 새 초기 상태는 정상이며 schedule 디렉터리를 만들지 않는다.
- `daemon.py`: ready와 health를 구분하고 실제 유지 작업 실패는 degraded로 출력.
- 새 회귀 두 파일. 기존 oversized profile·renderer·일일 요약은 변경하지 않았다.

### 실행 결과

| 검사 | 결과 |
|---|---|
| 수정 전 재현 | 최초 6개 중 3 FAIL, 부모 교체 외부 읽기와 doctor healthy/0 확인 |
| 최종 안전 회귀의 기존 커밋 음성 대조 | 7개 중 4 FAIL. 기존 source는 수정하지 않았고 새 회귀만 적용 |
| 관련 회귀 | 21 PASS |
| 전체 unittest | 148 PASS |
| 전체 pytest | 148 PASS + 31 subtests PASS |
| basedpyright | 0 errors / 0 warnings / 0 notes |
| 변경 Python no-excuse | 0 violations |
| compileall / git diff --check | PASS |
| 실제 CLI/PTY | invalid future cursor: doctor attention/exit1 + foreground degraded; 정상 fixture: healthy/exit0 + foreground healthy |

두 foreground child는 daemon PID와 대조한 뒤 SIGTERM/stopped/exit0/wait로 회수했다. file-fsync 실패의 이전 cursor 보존·temp 정리, 일일 latest 게시 실패의 cursor 미진행, 부모/leaf link/FIFO/성장·UTF-8 실패를 회귀로 확인했다. 초기 typecheck의 callback 반환값 경고3개는 수정 후0이며 오류를 숨기는 suppression을 추가하지 않았다.

이는 작성자 검증이지 Atlas 기술 인수/Vera 독립 QA/기본판 전체 수락은 아니다. 실제 사용자 기록·자연 OS race 빈도·reboot/power-loss는 NOT_RUN이며, file rename 뒤 directory-fsync 실패의 전원손실 상태까지 시험한 것으로 확대하지 않는다. renderer·일일 오류 구분·전체 장애 행렬과 기존 profile 분리는 여전히 별도 보강 항목이다.
