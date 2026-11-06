# 상주 기록 서비스 운영 안내

Agent Chronicle은 선택한 Pi session-v3 JSONL 폴더를 지속적으로 확인하는 **로컬 Linux 사용자 서비스**로 사용할 수 있습니다. 모든 에이전트의 모든 OS 행동을 감시하는 도구는 아닙니다. 숨겨진 추론·자격 증명을 수집 대상으로 삼지 않으며 패턴 마스킹이 모든 비밀정보를 제거한다는 보장도 없습니다.

현재 상주형은 [PR #2](https://github.com/DeclanJeon/agent-chronicle/pull/2)의 `feat/resident-recording` 개발판이며 main 미병합입니다. 이 브랜치 또는 같은 고정본을 사용해야 아래 동작과 일치합니다.

## 1. 설치와 최초 동의

Python 3.12 이상과 PyYAML이 준비된 환경에서 저장소를 오래 보관할 로컬 폴더에 둡니다. 의존성이 없으면 먼저 해당 환경을 준비해야 하며 서비스 설치가 패키지를 자동 다운로드하지 않습니다.

```bash
cd agent-chronicle
export PYTHONPATH="$PWD/src"
python3 -m chronicle init --timezone Asia/Seoul
python3 -m chronicle sources enable \
  --source pi-jsonl-v3 --root "/수집을허용한/Pi/로그폴더" --project my-work
python3 -m chronicle service install --preview
python3 -m chronicle service install --confirm
```

`init`은 저장 위치를 질문하며, 기본값은 `~/Documents/Obsidian/AgentHistory`입니다. 일반 폴더도 가능하며 Obsidian 설치는 필요하지 않습니다. source를 직접 등록하기 전에는 상주 서비스를 설치하지 않습니다. 전체 홈이나 파일시스템 루트는 허용 범위로 등록할 수 없습니다.

서비스 설치는 `~/.config/systemd/user/agent-chronicle.service`를 만들고 `systemctl --user enable --now`를 수행합니다. 따라서 **즉시 수집을 시작하며**, 사용자 manager가 다음에 시작될 때 default.target 의존성으로 자동 실행됩니다. sudo/시스템 전역 서비스/cron/linger 설정은 변경하지 않습니다.

설치 시 현재 Python 실행 파일, Chronicle import 경로, config/state/cache 절대 경로만 unit에 고정합니다. 로그인 셸의 PATH/PYTHONPATH나 전체 환경변수 복사에 의존하지 않습니다. OAuth/API 키를 unit에 복사하지 않습니다. 소스 폴더나 Python 가상환경을 삭제·이동하면 재시작할 수 없으므로 설치 위치를 유지해야 합니다.

## 2. 계속 작동하는 방식

- 기본적으로 약 1초마다 등록 범위를 다시 확인합니다. `--root` 범위에 새로 생긴 JSONL 세션과 기존 파일의 추가 레코드가 대상입니다.
- 완전하지 않은 JSONL/UTF-8은 pending으로 기다립니다. 손상된 완결 레코드는 실패를 표시하고 임의로 건너뛰지 않습니다.
- Markdown·체크포인트가 커밋된 진행 지점을 기준으로 재개합니다. 원본 로그가 남아 있으면 중지 중의 추가 기록도 뒤따라 수집합니다. 삭제/회전으로 사라진 바이트를 만들어 복원하지 않습니다.
- 사실 기반 일일 정리는 고정 시간대와 00:05 유예 기준으로 지난 날짜를 처리합니다. 오늘 노트는 `python3 -m chronicle daily`로 만들 수 있습니다.
- 프로세스가 죽거나 예기치 않게 정상 종료되어도 systemd가 5초 간격으로 재시작합니다. 영구적인 설정/디스크/입력 문제가 자동으로 고쳐진다는 뜻은 아닙니다. 반복 실패는 로그와 상태를 확인해 원인을 해결해야 합니다.
- 명시적인 systemctl/CLI stop·remove는 재시작 정책의 예외입니다. 컴퓨터 전원 OFF/절전 중에는 수집할 수 없습니다.

## 3. 상태와 로그

새 터미널에서 관리 CLI를 사용하려면 설치 폴더로 이동하고 `export PYTHONPATH="$PWD/src"`를 설정합니다. 이미 실행 중인 서비스에는 이 셸 설정이 필요하지 않습니다.

```bash
python3 -m chronicle service status
python3 -m chronicle status
python3 -m chronicle doctor
journalctl --user -u agent-chronicle.service --since today
journalctl --user -u agent-chronicle.service -f
```

| 필드/명령 | 뜻 |
|---|---|
| service `status` | systemd의 active/inactive/failed 등 프로세스 생명주기 |
| `health` | 최근 관측된 healthy/degraded/stopped. 프로세스 active가 기록 건강을 뜻하지 않음 |
| `enabled` | 로그인 사용자 manager 시작 경로에 등록되었는지 |
| `main_pid` | 실제 관리 프로세스 PID |
| `restart_count` | 현재 systemd unit 수명 동안의 자동 재시작 횟수. 영구 장애 이력 원장 아님 |
| Chronicle `status` | 파일별 cursor/pending/error/coverage gap |
| `doctor` | 저장소/체크포인트/FTS/일일 cursor 검사. 전체 누락 없음·운영 승인 보증 아님 |

서비스는 `READY=1` 통지 후 시작 성공을 확인하며, 첫 수집·유지 작업의 관측 health를 별도로 통지합니다. READY와 healthy는 같은 판정이 아닙니다. unit이 설치됐다는 사실과 실제 active/PID도 구분합니다. journal은 로컬 systemd 보존 정책을 따르며 외부 전송하지 않습니다. 자료를 공유하기 전 경로/개인정보를 검토하세요.

## 4. 중지, 재개, 제거

```bash
python3 -m chronicle service stop --confirm
python3 -m chronicle service restart --confirm
python3 -m chronicle service remove --confirm
```

- `stop`: 현재 실행 중지. enabled 상태는 유지합니다. 다시 시작하려면 `restart`를 사용합니다.
- `restart`: 실행 중이면 재시작, 중지 중이면 재개하고 준비 완료를 확인합니다.
- `remove`: disable/stop → 정확히 일치하는 unit 삭제 → daemon-reload. Vault/config/state/cache/원본 로그는 삭제하지 않습니다.
- 같은 unit/profile의 설치 재실행은 기존 프로세스를 불필요하게 재시작하지 않습니다.
- 다른 profile/runtime의 기존 unit을 덮어쓰거나 중지하지 않습니다. 해당 profile의 경로/실행 환경으로 관리하거나 기존 소유자에게 정리를 요청하세요. 임의 `--force`는 제공하지 않습니다.
- foreground `collect`와 상주 서비스를 동시에 실행하지 않습니다. 수집기 lock 충돌 시 새 설치는 거부합니다.

## 5. 검증 범위와 활성화 조건

개발 검증에서는 실제 사용자 systemd manager에 **합성 profile만 임시 설치**하여 로그인용 enable 링크, notify 준비 완료, 새 세션·append 수집, 프로세스 강제 종료/정상 신호 종료 뒤 자동 재시작, 입력 오류 degraded 및 fixture 복구 후 healthy, 명시 stop 유지, 중지 중 누적 후 따라잡기, replay 무중복, 기존 Markdown hash 보존, 제거 및 데이터 보존을 확인합니다. 셸에 의존하지 않는 이동된 소스/특수문자 경로도 시험합니다. 시험 서비스는 종료 후 disable/remove합니다.

실제 사용자 로그 수집의 지속 활성화와 실제 로그아웃/로그인·OS 재부팅·전원손실 시험은 별개입니다. 이 문서의 구현 설명이나 개발 시험을 그 운영 시험의 PASS로 확대하지 않습니다. 로그인 전 부팅 수집/로그아웃 후 상주를 위한 linger도 자동 활성화하지 않습니다.

실제 운영 시작에는 사용자가 선택한 **저장 폴더, 읽어도 되는 Pi 로그 폴더, 프로젝트 구분**이 필요합니다. 선택 없이 홈/세션 전체를 대신 연결하지 않습니다. 현재 전달은 구현·합성 시험 및 별도 독립 검토 단계이며 전체 제품 운영 수락과 구분합니다.
